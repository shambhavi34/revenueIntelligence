"""Deterministic fact packs - the only thing a model ever sees about a hotel.

Division of labour: code does arithmetic and rule pre-checks (remaining inventory, % vs comp, +/-15% price
band, group-confounded pickup, confirmed compression, consecutive-day runs). The model does judgment: which
dates matter, how to rank them, what to recommend, how to explain it. The post-check (guardrails.py) re-verifies
everything the model proposes against these same numbers, so a wrong or invented figure cannot reach a task.

PII: built from an allow-list of fields. Guest names/phones/emails are never read here.
"""
from __future__ import annotations

import json
import re
from datetime import date

import pandas as pd

from . import config as C
from .dq import Finding, rate_ref
from .ingest import Dataset


def _up(bar: int) -> int:  # integer math: no float surprises (4300 -> 4945)
    return bar * (100 + C.MAX_MOVE_PCT) // 100


def _down(bar: int) -> int:
    return -(-bar * (100 - C.MAX_MOVE_PCT) // 100)


def comp_runs(rows: list[dict]) -> dict:
    """Longest and trailing run of consecutive dates priced > COMP_GAP_PCT below comp-set average."""
    longest = cur = 0
    for r in rows:
        cur = cur + 1 if r["gap_vs_comp_pct"] < -C.COMP_GAP_PCT else 0
        longest = max(longest, cur)
    trailing = 0
    for r in reversed(rows):
        if r["gap_vs_comp_pct"] < -C.COMP_GAP_PCT:
            trailing += 1
        else:
            break
    return {"window_days": len(rows), "longest_run_below_10pct": longest, "trailing_run_below_10pct": trailing,
            "rule_threshold_days": C.COMP_RUN_DAYS,
            "dates_below_10pct": [r["date"] for r in rows if r["gap_vs_comp_pct"] < -C.COMP_GAP_PCT],
            "refs_below_10pct": [r["ref"] for r in rows if r["gap_vs_comp_pct"] < -C.COMP_GAP_PCT]}


def date_rows(ds: Dataset, hotel_id: str) -> list[dict]:
    m = ds.recon[ds.recon.hotel_id == hotel_id].sort_values("stay_date_s")
    out = []
    for x in m.itertuples():
        cap, sold = int(x.capacity), int(x.rooms_sold)
        remaining = cap - sold
        occ = round(sold / cap * 100, 1)
        gap = round((x.bar_inr - x.comp_set_avg_inr) / x.comp_set_avg_inr * 100, 1)
        group_rooms = int(x.pms_group)
        event = x.event_flag if str(x.event_flag).lower() != "none" else None
        compression = bool(event) and occ >= 80 and x.pickup_last_7d >= 10 and x.group_block_rooms == 0
        inc_allowed = remaining >= C.MIN_REMAINING_FOR_INCREASE or compression
        reason = None
        if not inc_allowed:
            reason = f"§4.4: only {remaining} rooms remain (<{C.MIN_REMAINING_FOR_INCREASE}) and no confirmed compression event"
        row = {
            "ref": rate_ref(x.ar_hotel_id, x.stay_date_s), "date": x.stay_date_s,
            "dow": date.fromisoformat(x.stay_date_s).strftime("%a"),
            "days_out": (date.fromisoformat(x.stay_date_s) - C.AS_OF.date()).days,
            "bar": int(x.bar_inr), "comp_avg": int(x.comp_set_avg_inr), "gap_vs_comp_pct": gap,
            "capacity": cap, "sold": sold, "remaining": remaining, "occ_pct": occ,
            "pickup_7d": int(x.pickup_last_7d), "forecast_rooms": int(x.forecast_rooms),
            "group_rooms": group_rooms, "transient_sold": sold - group_rooms,
            "pickup_usable_as_transient_signal": group_rooms == 0 and int(x.group_block_rooms) == 0,
            "ota_pct": int(x.channel_mix_ota_pct), "restrictions": x.restrictions or None,
            "event": event, "notes": x.notes or None,
            "high_demand": occ >= C.HIGH_DEMAND_OCC_PCT or bool(event) or group_rooms > 0,
            "compression_confirmed": compression, "increase_allowed": inc_allowed, "increase_block_reason": reason,
            "max_bar_up": _up(int(x.bar_inr)), "max_bar_down": _down(int(x.bar_inr)),
        }
        if group_rooms:
            row["group_rate"] = int(x.group_rate_inr)
        out.append(row)
    return out


def signal_findings(ds: Dataset) -> list[Finding]:
    """Findings where the data and the business rules interact (rule_vs_data)."""
    out: list[Finding] = []
    for hid in ds.hotel_ids():
        rows = date_rows(ds, hid)
        runs = comp_runs(rows)
        notes = " ".join(r["notes"] or "" for r in rows).lower()
        if runs["dates_below_10pct"] and "below comp" in notes and runs["longest_run_below_10pct"] < C.COMP_RUN_DAYS:
            out.append(Finding("warn", "rule_vs_data", "rate_pace.csv / business_rules_and_sops.md §4.6",
                               f"{hid}: revenue notes say BAR is 'consistently below comp set', but BAR is >10% below comp on "
                               f"{len(runs['dates_below_10pct'])}/{runs['window_days']} dates and the longest consecutive run is "
                               f"{runs['longest_run_below_10pct']} days (rule needs {C.COMP_RUN_DAYS}+)",
                               "Rules win: the mandatory Revenue Control Tower flag is NOT triggered by this window. "
                               "A pricing review is recommended instead, and the gap is stated.",
                               hotel_id=hid, refs=runs["refs_below_10pct"][:10],
                               llm_note=f"Notes say 'consistently below comp set' but the data shows a longest run of {runs['longest_run_below_10pct']} consecutive days >10% below comp (rule 4.6 needs 7+)."))
        for r in rows:
            if r["group_rooms"] and r["pickup_7d"] >= 10:
                out.append(Finding("info", "rule_vs_data", "rate_pace.csv / business_rules_and_sops.md §4.3",
                                   f"{hid} {r['date']}: pickup_7d={r['pickup_7d']} on a date with a {r['group_rooms']}-room group block",
                                   "Pickup treated as group-confounded and excluded as transient demand evidence (rule 4.3).",
                                   hotel_id=hid, refs=[r["ref"]],
                                   llm_note=f"{r['date']}: pickup of {r['pickup_7d']} coincides with a {r['group_rooms']}-room group block and cannot be attributed to transient demand."))
        for r in rows:
            if r["compression_confirmed"] and r["remaining"] < C.MIN_REMAINING_FOR_INCREASE:
                out.append(Finding("info", "rule_vs_data", "rate_pace.csv / business_rules_and_sops.md §4.4",
                                   f"{hid} {r['date']}: only {r['remaining']} rooms remain, but a confirmed compression event ({r['event']}; "
                                   f"occupancy {r['occ_pct']}%, pickup {r['pickup_7d']}, no group block) lifts the <10-room increase block",
                                   "Increase allowed, capped at +15% (rule 4.2).", hotel_id=hid, refs=[r["ref"]]))
    return out


def _group_blocks(ds: Dataset, hotel_id: str) -> list[dict]:
    g = ds.bookings[(ds.bookings.hotel_id == hotel_id) & ds.bookings.is_group & ~ds.bookings.dq_excluded]
    out = []
    for code, gg in g.groupby("group_code", dropna=False):
        ids = sorted(gg.booking_id)
        out.append({"code": code if isinstance(code, str) else None, "booking_rows": len(gg), "arrive": gg.arrival_date.min(),
                    "depart": gg.departure_date.max(), "adr": int(gg.adr_inr.astype(int).median()),
                    "channel": gg.channel.mode().iloc[0], "example_booking_ids": [ids[0], ids[-1]]})
    return out


def _plan_view(ds: Dataset, hotel_id: str, rows: list[dict]) -> None:
    """Adds plan mix + lowest-ADR booking ids to each date row (restriction agent only)."""
    n = ds.nights[(ds.nights.hotel_id == hotel_id) & ~ds.nights.is_group & ~ds.nights.created_after_as_of]
    for r in rows:
        d = n[n.stay_date == r["date"]]
        plans = {p: [int(len(g)), int(round(g.adr_inr.mean()))] for p, g in d.groupby("rate_plan")}
        r["transient_plans_n_avg_adr"] = plans
        if r["high_demand"]:
            low = d.sort_values(["adr_inr", "booking_id"]).head(3)
            r["lowest_adr_transient_bookings"] = [[b.booking_id, b.rate_plan, b.channel, int(b.adr_inr)] for b in low.itertuples()]


def build_facts(ds: Dataset, findings: list[Finding], hotel_id: str, agent: str) -> dict:
    h = ds.master[ds.master.hotel_id == hotel_id].iloc[0]
    rows = date_rows(ds, hotel_id)
    if agent == "channel_inventory_restriction":
        _plan_view(ds, hotel_id, rows)
    warn = [f for f in findings if f.hotel_id in (hotel_id, None) and f.llm_note and f.severity in ("warn", "high", "info")]
    facts = {
        "hotel": {"hotel_id": hotel_id, "name": h.hotel_name, "brand": h.brand, "city": h.city, "capacity": int(h.rooms),
                  "ref": hotel_id},
        "as_of": C.AS_OF_ISO, "horizon": [rows[0]["date"], rows[-1]["date"]],
        "dates": rows,
        "comp_set": comp_runs(rows),
        "group_blocks": _group_blocks(ds, hotel_id),
        "data_warnings": [{"id": f.id, "severity": f.severity, "text": f.llm_note} for f in warn],
        "allowed_actions": {"discount_plans_closable": sorted(C.DISCOUNT_PLANS)} if agent == "channel_inventory_restriction" else {},
    }
    return facts


# ----- reference registry & numeric grounding ---------------------------------------------------
def allowed_refs(facts: dict) -> dict[str, set[str]]:
    """Every record reference the model may cite = those that appear in the facts it was given."""
    rate = {r["ref"] for r in facts["dates"]}
    bookings: set[str] = set()
    for g in facts["group_blocks"]:
        bookings.update(g["example_booking_ids"])
    for r in facts["dates"]:
        for b in r.get("lowest_adr_transient_bookings", []):
            bookings.add(b[0])
    return {
        "rate_pace.csv": rate,
        "bookings.csv": bookings,
        "property_master.csv": {facts["hotel"]["hotel_id"]},
        "business_rules_and_sops.md": set(C.RULES),
        "data_quality_report": {w["id"] for w in facts["data_warnings"]},
    }


NUM_RE = re.compile(r"(?<![\w.,-])\d{1,3}(?:,\d{3})+(?![\w-])|(?<![\w.,-])\d{3,7}(?![\w.-])")


def numbers_in(text: str) -> set[int]:
    return {int(m.replace(",", "")) for m in NUM_RE.findall(text)}


def fact_numbers(facts: dict) -> set[int]:
    return numbers_in(json.dumps(facts, ensure_ascii=False))
