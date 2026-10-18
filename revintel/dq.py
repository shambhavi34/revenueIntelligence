"""Data-quality checks and report.

Principles (from the brief): never drop rows silently, never invent a join.
Every finding states what was found, which records it touches, and *what the pipeline did about it*.

Severity:
  high - blocks or could corrupt a decision (unresolved key, PMS over capacity)
  warn - changes how much an agent should trust a number
  info - transparency (assumptions, benign differences)

`llm_note` is the short, PII-free sentence that is allowed to reach the model (facts.py).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import pandas as pd

from . import config as C


@dataclass
class Finding:
    severity: str
    category: str
    source: str
    summary: str
    action_taken: str
    hotel_id: str | None = None
    detail: str = ""
    refs: list[str] = field(default_factory=list)
    llm_note: str | None = None
    id: str = ""

    def to_dict(self):
        return asdict(self)


def _refs(ids, cap=25):
    ids = list(ids)
    return ids[:cap] + ([f"... +{len(ids) - cap} more"] if len(ids) > cap else [])


def rate_ref(ar_id: str, d) -> str:
    return f"{ar_id}|{d}"


# ---------------------------------------------------------------------------
def check_master(master: pd.DataFrame) -> list[Finding]:
    out: list[Finding] = []
    for col in ("hotel_id", "pms_property_code", "ar_hotel_id"):
        dups = master[master.duplicated(col, keep=False)]
        if len(dups):
            out.append(Finding("high", "ambiguous_join", "property_master.csv",
                               f"Crosswalk key {col} is not unique in the master",
                               f"Records using the duplicated key are quarantined, not guessed.",
                               refs=_refs(dups["hotel_id"])))
    for _, r in master.iterrows():
        if not str(r.get("kepsla_property_slug", "")).strip():
            out.append(Finding("info", "crosswalk_gap", "property_master.csv",
                               f"{r.hotel_id}: Kepsla slug is blank in the master",
                               "No effect on revenue files (they key on PMS code / AxisRooms id). "
                               "Review-system joins for this hotel would be unresolved.",
                               hotel_id=r.hotel_id, refs=[r.hotel_id]))
        # name vs slug: compare non-brand tokens
        name_t = set(re.findall(r"[a-z]+", str(r.hotel_name).lower())) - {"hotels", "hotel", "the"}
        slug_t = set(re.findall(r"[a-z]+", str(r.kepsla_property_slug).lower()))
        brand_t = set(re.findall(r"[a-z]+", str(r.brand).lower()))
        if slug_t and not ((name_t - brand_t) & slug_t):
            out.append(Finding("info", "name_mismatch", "property_master.csv",
                               f"{r.hotel_id}: hotel_name '{r.hotel_name}' shares no location token with slug "
                               f"'{r.kepsla_property_slug}'",
                               "Identity is resolved through ids only; names are never used as join keys.",
                               hotel_id=r.hotel_id, refs=[r.hotel_id]))
    return out


def check_resolution(n_b, n_b_ok, n_r, n_r_ok, norm_changes: dict[str, int],
                     quarantined_b: pd.DataFrame, quarantined_r: pd.DataFrame) -> list[Finding]:
    out = [Finding("info", "resolution_summary", "bookings.csv / rate_pace.csv",
                   f"Resolved {n_b_ok}/{n_b} bookings and {n_r_ok}/{n_r} rate_pace rows to a hotel_id via the master crosswalk",
                   "Exact match on pms_property_code / ar_hotel_id only (after trimming whitespace/case). No fuzzy matching.")]
    if len(quarantined_b):
        out.append(Finding("high", "unresolved_key", "bookings.csv",
                           f"{len(quarantined_b)} booking rows could not be resolved to a hotel",
                           "Quarantined to bookings_quarantine with a reason; excluded from every metric; not guessed.",
                           refs=_refs(quarantined_b["booking_id"])))
    if len(quarantined_r):
        out.append(Finding("high", "unresolved_key", "rate_pace.csv",
                           f"{len(quarantined_r)} rate_pace rows could not be resolved to a hotel",
                           "Quarantined to rate_pace_quarantine; excluded from every metric; not guessed.",
                           refs=_refs(quarantined_r["ar_hotel_id"] + "|" + quarantined_r["stay_date"])))
    for k, v in norm_changes.items():
        if v:
            out.append(Finding("info", "key_normalisation", k, f"{v} key values trimmed/case-normalised before matching",
                               "Pure formatting normalisation, logged here."))
    return out


def check_duplicates(b: pd.DataFrame) -> list[Finding]:
    out: list[Finding] = []
    d = b[b.duplicated("booking_id", keep=False)]
    if len(d):
        out.append(Finding("high", "duplicate", "bookings.csv", f"{d.booking_id.nunique()} booking_ids appear more than once",
                           "Identical copies: later copies dropped from metrics (kept in table, dq_excluded=1). "
                           "Conflicting copies: kept first, rest quarantined.", refs=_refs(d.booking_id.unique())))
    content_cols = [c for c in b.columns if c not in ("booking_id", "booking_created_at", "created_at_ist", "created_after_as_of", "dq_excluded", "dup_of")]
    cd = b[b.duplicated(content_cols, keep=False)]
    if len(cd):
        out.append(Finding("high", "duplicate", "bookings.csv", f"{len(cd)} rows are content-identical under different booking_ids",
                           "Later copies flagged dq_excluded=1 and left out of counts.", refs=_refs(cd.booking_id)))
    key = ["hotel_id", "guest_id", "arrival_date", "departure_date"]
    pairs = b[b.duplicated(key, keep=False)].sort_values(key)
    for _, g in pairs.groupby(key):
        names_differ = g["guest_name_masked"].nunique() > 1
        out.append(Finding("warn", "possible_duplicate", "bookings.csv",
                           f"{g.hotel_id.iloc[0]}: {len(g)} bookings share guest_id, hotel and stay dates "
                           f"({g.arrival_date.iloc[0]} to {g.departure_date.iloc[0]})"
                           + ("; guest names differ" if names_differ else ""),
                           "NOT merged (names/channels/rates differ, so a merge would be invented). Both kept; "
                           "inventory counts include both.",
                           hotel_id=g.hotel_id.iloc[0], refs=list(g.booking_id),
                           llm_note=f"Bookings {', '.join(g.booking_id)} share guest_id and dates; treated as separate bookings."))
    multi = b.groupby("guest_id")["guest_name_masked"].nunique()
    if (multi > 1).sum():
        out.append(Finding("info", "identity_reuse", "bookings.csv",
                           f"{int((multi > 1).sum())} guest_ids map to more than one guest name",
                           "guest_id is not trusted as a person key; it is never used to merge or de-duplicate records."))
    return out


def check_status_and_text(raw_b: pd.DataFrame, b: pd.DataFrame) -> list[Finding]:
    out: list[Finding] = []
    known = {"Confirmed", "In-House"}
    extra = sorted(set(b.status) - known)
    if extra:
        out.append(Finding("info", "schema_drift", "bookings.csv",
                           f"status values {extra} appear but DATA_DICTIONARY lists only Confirmed / In-House "
                           f"({int(b.status.isin(extra).sum())} rows)",
                           "Kept as-is. Checked-Out rows end on/before 25 Aug so they never occupy a forward night."))
    out.append(Finding("info", "pii", "bookings.csv",
                       f"Structured PII present on {len(raw_b)} rows (guest_name, guest_phone, guest_email)",
                       "Names/phones masked and emails dropped in the stored view; raw values never enter prompts "
                       "(prompt scanner enforces this)."))
    from .pii import EMAIL_RE, PHONE_RE
    hits = 0
    for col in ("special_request", "pms_notes"):
        hits += int(raw_b[col].astype(str).map(lambda s: bool(EMAIL_RE.search(s) or PHONE_RE.search(s))).sum())
    out.append(Finding("info", "pii", "bookings.csv",
                       f"Phone/email regex hits in free text (special_request, pms_notes): {hits}",
                       "DATA_DICTIONARY warns of PII in free text; none matched these patterns, but free text is still "
                       "scrubbed (phones, emails, own-guest name tokens) before storage and is never sent to a model."))
    tiers = set(b.loyalty_tier) - {""}
    if "Nova Rewards Gold" not in tiers:
        out.append(Finding("info", "rule_vs_data", "bookings.csv",
                           "Rules (section 1) mention Nova Rewards Gold; data only has " + ", ".join(sorted(tiers)),
                           "No effect on revenue logic."))
    return out


def check_late_bookings(b: pd.DataFrame) -> list[Finding]:
    late = b[b.created_after_as_of]
    if not len(late):
        return []
    per_hotel = late.groupby("hotel_id").size().to_dict()
    return [Finding("warn", "time_leakage", "bookings.csv",
                    f"{len(late)} bookings have booking_created_at after the 25 Aug 09:00 IST briefing time "
                    f"(latest {late.created_at_ist.max()}); per hotel: {per_hotel}",
                    "Kept and flagged created_after_as_of=1. Excluded from as-of recounts. Because rate_pace.rooms_sold "
                    "tracks the full PMS count, AxisRooms is treated as 'now' but its as-of integrity is unverified.",
                    refs=_refs(late.booking_id),
                    llm_note=f"{len(late)} PMS bookings are timestamped after the briefing time; rate_pace figures may include them.")]


def reconcile_rate_vs_pms(master, rate: pd.DataFrame, nights: pd.DataFrame, b: pd.DataFrame) -> tuple[pd.DataFrame, list[Finding]]:
    """Returns per hotel-date reconciliation frame + findings."""
    cap = master.set_index("hotel_id")["rooms"].astype(int).to_dict()
    ar = master.set_index("hotel_id")["ar_hotel_id"].to_dict()
    n = nights.copy()
    n["is_transient"] = ~n.is_group
    g = n.groupby(["hotel_id", "stay_date"])
    rec = pd.DataFrame({
        "pms_all": g.size(),
        "pms_as_of": n[~n.created_after_as_of].groupby(["hotel_id", "stay_date"]).size(),
        "pms_group": n[n.is_group].groupby(["hotel_id", "stay_date"]).size(),
        "pms_late": n[n.created_after_as_of].groupby(["hotel_id", "stay_date"]).size(),
    }).fillna(0).astype(int).reset_index()
    # OTA share among as-of transient bookings
    t = n[(~n.created_after_as_of) & n.is_transient]
    ota = t.assign(is_ota=t.channel.isin(C.OTA_CHANNELS)).groupby(["hotel_id", "stay_date"])["is_ota"].agg(["sum", "count"]).reset_index()
    ota["pms_ota_pct"] = (ota["sum"] / ota["count"] * 100).round(1)
    rec = rec.merge(ota[["hotel_id", "stay_date", "pms_ota_pct"]], on=["hotel_id", "stay_date"], how="left")
    # PMS created in the 7 days to as_of (all plans), per stay night
    w = n[(n.created_at >= pd.Timestamp(C.AS_OF.replace(tzinfo=None) - timedelta(days=7))) & (~n.created_after_as_of)]
    p7 = w.groupby(["hotel_id", "stay_date"]).size().rename("pms_created_7d").reset_index()
    rec = rec.merge(p7, on=["hotel_id", "stay_date"], how="left").fillna({"pms_created_7d": 0})

    r = rate.copy()
    r["stay_date_s"] = r["stay_date"].astype(str)
    rec["stay_date_s"] = rec["stay_date"].astype(str)
    m = r.merge(rec.drop(columns="stay_date"), on=["hotel_id", "stay_date_s"], how="left")
    for c in ("pms_all", "pms_as_of", "pms_group", "pms_late"):
        m[c] = m[c].fillna(0).astype(int)
    m["capacity"] = m.hotel_id.map(cap)

    out: list[Finding] = []
    # capacity semantics
    if (m.rooms_available == m.capacity).all():
        out.append(Finding("info", "assumption", "rate_pace.csv",
                           "rooms_available equals the master room count on every row, i.e. it is capacity, not remaining inventory",
                           "remaining = rooms_available - rooms_sold. Out-of-order rooms are not in any file, so saleable "
                           "inventory is an upper bound.",
                           llm_note="rooms_available is total capacity; remaining = capacity - rooms_sold. No out-of-order data is available."))
    # PMS over capacity
    for _, x in m[m.pms_all > m.capacity].iterrows():
        rr = rate_ref(x.ar_hotel_id, x.stay_date_s)
        late_ids = nights[(nights.hotel_id == x.hotel_id) & (nights.stay_date.astype(str) == x.stay_date_s) & nights.created_after_as_of].booking_id
        out.append(Finding("high", "over_capacity", "bookings.csv",
                           f"{x.hotel_id} {x.stay_date_s}: PMS holds {x.pms_all} bookings for a {x.capacity}-room hotel "
                           f"(AxisRooms rooms_sold={x.rooms_sold}; {x.pms_late} of the PMS bookings were created after as_of)",
                           "Flagged; no automatic fix. A reconciliation task is raised for the Revenue Manager. "
                           "Agents are told the inventory position on this date is unreliable.",
                           hotel_id=x.hotel_id, refs=[rr] + _refs(late_ids, 12),
                           llm_note=f"{x.stay_date_s}: PMS shows {x.pms_all} bookings vs capacity {x.capacity} and rooms_sold {x.rooms_sold} in rate_pace; inventory position is unreliable."))
    # sold vs PMS
    mm = m[m.rooms_sold != m.pms_all]
    eq = int((m.rooms_sold == m.pms_all).sum())
    asof_eq = int((m.rooms_sold == m.pms_as_of).sum())
    out.append(Finding("info", "reconciliation", "rate_pace.csv vs bookings.csv",
                       f"rooms_sold equals the full PMS night-count on {eq}/{len(m)} rows but the as-of count on only {asof_eq}/{len(m)}",
                       "rate_pace is treated as the current position but is not an as-of-09:00 snapshot."))
    for _, x in mm.iterrows():
        out.append(Finding("warn", "reconciliation", "rate_pace.csv vs bookings.csv",
                           f"{x.hotel_id} {x.stay_date_s}: rooms_sold={x.rooms_sold} vs PMS {x.pms_all} (as-of {x.pms_as_of})",
                           "Flagged; rate_pace value used as given, with a data-gap note to the agents.",
                           hotel_id=x.hotel_id, refs=[rate_ref(x.ar_hotel_id, x.stay_date_s)]))
    # occupancy recompute
    bad = m[(m.rooms_sold / m.rooms_available * 100 - m.occupancy_pct).abs() > 0.15]
    if len(bad):
        out.append(Finding("warn", "arithmetic", "rate_pace.csv", f"{len(bad)} rows where occupancy_pct != sold/available",
                           "Occupancy recomputed from sold/capacity in facts.",
                           refs=[rate_ref(a, d) for a, d in zip(bad.ar_hotel_id, bad.stay_date_s)]))
    # forecast below sold
    for hid, g_ in m[m.forecast_rooms < m.rooms_sold].groupby("hotel_id"):
        ex = g_.sort_values("stay_date_s")
        out.append(Finding("warn", "semantic", "rate_pace.csv",
                           f"{hid}: forecast_rooms is below rooms already sold on {len(ex)}/{int((m.hotel_id == hid).sum())} dates "
                           f"(e.g. {ex.stay_date_s.iloc[0]}: forecast {int(ex.forecast_rooms.iloc[0])} < sold {int(ex.rooms_sold.iloc[0])})",
                           "forecast_rooms is not usable as a forecast of final occupancy; agents are told to rely on sold, pickup and "
                           "remaining inventory, and to say so.",
                           hotel_id=hid, refs=[rate_ref(a, d) for a, d in zip(ex.ar_hotel_id, ex.stay_date_s)][:10],
                           llm_note=f"forecast_rooms is below rooms_sold on {len(ex)} of {int((m.hotel_id == hid).sum())} dates, so it cannot be a final-occupancy forecast."))
    # channel mix reconciliation
    for hid, g_ in m.groupby("hotel_id"):
        d = (g_.pms_ota_pct - g_.channel_mix_ota_pct).abs()
        far = g_[d > 15]
        if len(far) >= 3:
            out.append(Finding("warn", "semantic", "rate_pace.csv vs bookings.csv",
                               f"{hid}: channel_mix_ota_pct differs from the PMS-derived OTA share by >15 points on {len(far)}/{len(g_)} dates",
                               "rate_pace.channel_mix_ota_pct is used as given for the section 4.5 check but marked unverified "
                               "(its definition is not documented; OTA channel set is an assumption).",
                               hotel_id=hid, refs=[rate_ref(a, dt) for a, dt in zip(far.ar_hotel_id, far.stay_date_s)][:10],
                               llm_note=f"channel_mix_ota_pct could not be reproduced from PMS channels on {len(far)} of {len(g_)} dates; treat as unverified."))
    # pickup reconciliation
    for hid, g_ in m.groupby("hotel_id"):
        far = g_[(g_.pickup_last_7d - g_.pms_created_7d).abs() > 5]
        if len(far) >= 3:
            out.append(Finding("info", "semantic", "rate_pace.csv vs bookings.csv",
                               f"{hid}: pickup_last_7d differs from PMS bookings created in the prior 7 days by >5 rooms on {len(far)}/{len(g_)} dates",
                               "Net pickup can legitimately differ (cancellations are not in the PMS export). Used as given, marked unverified.",
                               hotel_id=hid, refs=[rate_ref(a, dt) for a, dt in zip(far.ar_hotel_id, far.stay_date_s)][:10],
                               llm_note=f"pickup_last_7d could not be reproduced from PMS creation dates on {len(far)} of {len(g_)} dates; treat as unverified."))
    return m, out


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def check_events_and_groups(rate: pd.DataFrame, b: pd.DataFrame, master) -> list[Finding]:
    out: list[Finding] = []
    ar = master.set_index("hotel_id")["ar_hotel_id"].to_dict()
    # event window vs flag
    for _, e in rate[rate.event_flag.str.lower() != "none"].drop_duplicates(["hotel_id", "event_flag"]).iterrows():
        m = re.search(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]{3})", e.event_flag)
        if not m:
            continue
        mon = MONTHS.get(m.group(3).lower())
        if not mon:
            continue
        lo, hi = date(2026, mon, int(m.group(1))), date(2026, mon, int(m.group(2)))
        sub = rate[(rate.hotel_id == e.hotel_id)]
        inwin = sub[(pd.to_datetime(sub.stay_date).dt.date >= lo) & (pd.to_datetime(sub.stay_date).dt.date <= hi)]
        unflagged = inwin[inwin.event_flag.str.lower() == "none"]
        if len(unflagged):
            out.append(Finding("warn", "semantic", "rate_pace.csv",
                               f"{e.hotel_id}: event '{e.event_flag}' spans {lo}..{hi} but event_flag is 'none' on "
                               f"{', '.join(unflagged.stay_date.astype(str))}",
                               "Event dates taken from the flagged row only; unflagged dates are not assumed to be event-driven.",
                               hotel_id=e.hotel_id, refs=[rate_ref(ar[e.hotel_id], d) for d in unflagged.stay_date.astype(str)],
                               llm_note=f"Event '{e.event_flag}' spans {lo}..{hi} but only some of those dates carry the flag in rate_pace."))
    # group block consistency
    g = b[b.is_group]
    for (hid, code), gg in g.groupby(["hotel_id", "group_code"], dropna=False):
        contracted = gg.contracted_on.dropna().unique()
        if len(contracted):
            c_date = pd.to_datetime(contracted[0]).date()
            diff = gg[pd.to_datetime(gg.created_at_ist).dt.date != c_date]
            after = gg[gg.created_after_as_of]
            if len(diff):
                out.append(Finding("warn", "semantic", "bookings.csv",
                                   f"{hid}: group block {code} is noted as contracted {c_date}, but {len(diff)}/{len(gg)} of its booking rows "
                                   f"have other creation dates ({gg.created_at_ist.min()[:10]} to {gg.created_at_ist.max()[:10]}); {len(after)} are after as_of",
                                   "Block size/rate taken from rate_pace + rate_plan=GROUP-CONTRACT; creation timestamps not used as evidence of contract date.",
                                   hotel_id=hid, refs=_refs(gg.booking_id, 6),
                                   llm_note=f"Group block {code}: PMS row creation dates conflict with the 'contracted {c_date}' note; contract date unverified."))
        # rate_pace vs PMS group nights
        nights = {}
        for _, x in gg.iterrows():
            for d in pd.date_range(x.arrival_date, pd.Timestamp(x.departure_date) - pd.Timedelta(days=1)):
                nights[d.date().isoformat()] = nights.get(d.date().isoformat(), 0) + 1
        rp = rate[(rate.hotel_id == hid) & (rate.group_block_rooms.astype(int) > 0)]
        mism = [(d, int(n_), nights.get(str(d))) for d, n_ in zip(rp.stay_date.astype(str), rp.group_block_rooms.astype(int)) if nights.get(str(d)) != n_]
        out.append(Finding("info" if not mism else "warn", "reconciliation", "rate_pace.csv vs bookings.csv",
                           f"{hid}: group block {code} - rate_pace group_block_rooms "
                           + ("matches PMS GROUP-CONTRACT rows on every block night" if not mism else f"disagrees with PMS on {mism}"),
                           "Group rooms are excluded from pickup-based demand signals (rule 4.3).", hotel_id=hid,
                           refs=_refs(gg.booking_id, 4)))
    return out
