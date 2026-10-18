"""Central configuration. Every business constant traces to business_rules_and_sops.md.

Nothing here talks to a model. The LLM never sees or decides these numbers; code enforces them.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("REVINTEL_DATA_DIR", ROOT / "data"))
OUT_DIR = Path(os.getenv("REVINTEL_OUT_DIR", ROOT / "outputs"))
CACHE_DIR = OUT_DIR / "llm_cache"
DB_PATH = OUT_DIR / "revintel.db"

IST = timezone(timedelta(hours=5, minutes=30))
# "Treat that as now" - DATA_DICTIONARY.md. The system clock is never used.
AS_OF = datetime(2026, 8, 25, 9, 0, tzinfo=IST)
AS_OF_ISO = AS_OF.isoformat()  # 2026-08-25T09:00:00+05:30

MODEL = os.getenv("REVINTEL_MODEL", "claude-sonnet-5-5")
# Override with REVINTEL_OPENAI_MODEL if your account has a newer/cheaper model. The post-check does not depend on which model is used.
OPENAI_MODEL = os.getenv("REVINTEL_OPENAI_MODEL", "gpt-4o")

# --- Section 4: rate and revenue guardrails -------------------------------
MAX_MOVE_PCT = 15                 # 4.2  max single-move BAR change, +/-15%
MIN_REMAINING_FOR_INCREASE = 10   # 4.4  below this, no increase unless confirmed compression
HIGH_DEMAND_OCC_PCT = 80          # assumption: a "high-demand date" = occupancy >= 80%, or an event flag, or a group block
OTA_VISIBILITY_THRESHOLD = 60     # 4.5  channel_mix_ota_pct above this => visibility-risk flag
COMP_GAP_PCT = 10                 # 4.6  priced >10% below comp-set average ...
COMP_RUN_DAYS = 7                 # 4.6  ... for 7+ consecutive days => flag Revenue Control Tower

# Section 4 bullets, numbered so agents can cite them as evidence (source=business_rules_and_sops.md).
RULES = {
    "§4.1": "BAR may be moved by an agent recommendation only; all rate changes require Revenue Manager approval before pushing to AxisRooms.",
    "§4.2": "Maximum single-move BAR change is +/-15% of current BAR; anything larger is an exception requiring Ops Head sign-off.",
    "§4.3": "Group-contracted rooms are excluded from pickup-based demand signals; pickup driven by a contracted block is not evidence of transient demand.",
    "§4.4": "Do not recommend a BAR increase when remaining saleable transient inventory is below 10 rooms unless there is a confirmed compression event.",
    "§4.5": "Do not recommend closing the lowest rate plan when channel_mix_ota_pct exceeds 60% without flagging visibility risk.",
    "§4.6": "A property priced more than 10% below comp-set average for 7+ consecutive days must be flagged to the Revenue Control Tower.",
    "§7": "Revenue Manager receives all rate and inventory actions.",
    "§8": "Model outputs must not assert facts absent from the supplied data; if data is insufficient, state a gap and lower confidence.",
    "§9": "Every task needs evidence_refs pointing at real record identifiers from the source data.",
}

# --- Assumption (documented in README): OTA vs direct channel classification ----
# rate_pace.channel_mix_ota_pct has no published definition. For *reconciliation only*
# (never for decisions) we treat these PMS channels as OTA.
OTA_CHANNELS = {"MakeMyTrip", "Goibibo", "Airbnb", "Expedia", "Booking.com", "Agoda"}

# Assumption: "discount rates" that agents may recommend closing. BAR plans are not discounts; CORP-NEG is
# contractually negotiated; GROUP-CONTRACT is contracted. Plan semantics are inferred from names (flagged in README).
DISCOUNT_PLANS = {"OTA-PROMO", "ADV21-CP"}

# --- Task layer ----------------------------------------------------------
# business_rules_and_sops.md has no revenue-task SLA. Assumption: reuse the resolution SLA
# column of section 2 (P2 12h, P3 24h, P4 48h) as the "SLA-derived" clock for revenue tasks.
PRIORITY_FROM_URGENCY = {"High": "P2", "Medium": "P3", "Low": "P4"}
SLA_HOURS = {"P1": 4, "P2": 12, "P3": 24, "P4": 48}
MIN_LEAD_HOURS = 2  # a task for a same-day stay date is never due sooner than as_of + 2h

SOURCES = {
    "rate_pace.csv",
    "bookings.csv",
    "property_master.csv",
    "business_rules_and_sops.md",
    "data_quality_report",
}

OWNER_ROLES = {  # section 7 escalation matrix
    "Revenue Manager",
    "Operations Head",
    "General Manager",
}
