"""PII handling.

Policy (business_rules_and_sops.md section 8): the model gets guest_id and booking_id only.
Names, phones and emails never enter a prompt. We enforce that two ways:

1. Facts are built from an allow-list of fields (see facts.py); PII columns are never read.
2. `assert_prompt_clean` scans every outgoing prompt against *every* PII value in the raw
   export and raises if any appears. A refactor that leaks a name fails loudly, in tests and at runtime.

Stored data is masked too (name/phone masked, email dropped) so the SQLite view is safe to demo.
"""
from __future__ import annotations

import re
from typing import Iterable

import pandas as pd

PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s-]?)?\d{5}[\s-]?\d{5}(?!\w)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def mask_name(name: str) -> str:
    """'Rohit Balakrishnan' -> 'R***t B**********n' (first + last letter kept per token)."""
    out = []
    for tok in str(name).split():
        out.append(tok if len(tok) <= 2 else tok[0] + "*" * (len(tok) - 2) + tok[-1])
    return " ".join(out)


def mask_phone(phone: str) -> str:
    """'+91 9845012277' -> '+91 ****012277' (format used in rules section 8)."""
    digits = re.sub(r"\D", "", str(phone))
    return f"+91 ****{digits[-6:]}" if len(digits) >= 6 else "****"


def scrub_text(text: str, names: Iterable[str] = ()) -> str:
    """Remove phones, emails and the row's own guest-name tokens from free text."""
    t = EMAIL_RE.sub("[email]", str(text))
    t = PHONE_RE.sub("[phone]", t)
    for n in names:
        for tok in str(n).split():
            if len(tok) > 2:
                t = re.sub(re.escape(tok), mask_name(tok), t, flags=re.IGNORECASE)
    return t


def pii_values(raw_bookings: pd.DataFrame) -> dict[str, set[str]]:
    """Every raw PII value in the export, for leak scanning."""
    names, phones, emails = set(), set(), set()
    for v in raw_bookings["guest_name"].astype(str):
        names.add(v.strip())
    for v in raw_bookings["guest_phone"].astype(str):
        phones.add(v.strip())
        phones.add(re.sub(r"\D", "", v)[-10:])
    for v in raw_bookings["guest_email"].astype(str):
        emails.add(v.strip().lower())
    return {"names": {x for x in names if x}, "phones": {x for x in phones if x}, "emails": {x for x in emails if x}}


class PIILeak(RuntimeError):
    pass


TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'.-]*")
PHONE_CAND_RE = re.compile(r"\d[\d\s-]{8,}\d")


def find_pii(text: str, pii: dict[str, set[str]]) -> list[tuple[str, str]]:
    """Linear-time scan using whole-token n-grams / digit runs / email tokens (so 'Neha Reddy' never fires inside 'Sneha Reddy')."""
    hits: list[tuple[str, str]] = []
    # names: compare n-grams of word tokens
    names = {" ".join(v.split()).lower(): v for v in pii.get("names", ())}
    sizes = {len(k.split()) for k in names}
    toks = [t.lower() for t in TOKEN_RE.findall(text)]
    for n in sizes:
        for i in range(len(toks) - n + 1):
            g = " ".join(toks[i:i + n])
            if g in names:
                hits.append(("names", names[g]))
    # emails
    emails = {v.lower() for v in pii.get("emails", ())}
    for m in EMAIL_RE.findall(text):
        if m.lower() in emails:
            hits.append(("emails", m))
    # phones: last 10 digits of any long digit run
    phones = {re.sub(r"\D", "", v)[-10:] for v in pii.get("phones", ())}
    for m in PHONE_CAND_RE.findall(text):
        d = re.sub(r"\D", "", m)[-10:]
        if d in phones:
            hits.append(("phones", m))
    return hits


def assert_prompt_clean(prompt: str, pii: dict[str, set[str]]) -> None:
    hits = find_pii(prompt, pii)
    if hits:
        raise PIILeak(f"PII ({hits[0][0]}) found in prompt - refusing to send")
    if EMAIL_RE.search(prompt) or PHONE_RE.search(prompt):
        raise PIILeak("phone/email-shaped string found in prompt - refusing to send")
