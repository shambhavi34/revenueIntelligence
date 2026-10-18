"""Eval cases: assertions about what the agents must (and must never) do on this dataset.

They check *final* outputs (after the post-check), so they are provider-agnostic: run them against the mock, a replayed
cache, or a live model:   python -m evals.run --provider anthropic
A failing case = a regression you would not want to find after a bad rate was pushed live.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from revintel import config as C
from revintel import facts as F
from revintel.contract import AgentOutput

HYD, NOV, BLR, PNQ = "ACM-HYD-GCB-03", "NOV-BLR-WFD-02", "ACM-BLR-IND-01", "PRT-PNQ-AMB-04"


@dataclass
class Case:
    id: str
    why: str
    fn: callable


def _out(res, hotel, agent):
    return next(o for o in res["outputs"] if o["hotel_id"] == hotel and o["agent"] == agent)


def _increases(res):
    for o in res["outputs"]:
        if o["agent"] != "pricing_optimization":
            continue
        for a in o["recommended_actions"]:
            m = re.match(r"Increase BAR from INR (\d+) to INR (\d+) for (\d{4}-\d{2}-\d{2})", a["action"])
            if m:
                yield o["hotel_id"], int(m.group(1)), int(m.group(2)), m.group(3)


def identity_resolves(res):
    ds = res["ds"]
    assert len(ds.bookings_quarantine) == 0 and len(ds.rate_quarantine) == 0, "unexpected quarantine"
    assert len(ds.bookings) + len(ds.bookings_quarantine) == 1677, "booking rows lost"
    assert len(ds.rate) + len(ds.rate_quarantine) == 40, "rate rows lost"


def nova_no_increase_on_group_nights(res):
    bad = [(h, d, p) for h, c, p, d in _increases(res) if h == NOV and d in ("2026-08-27", "2026-08-28")]
    assert not bad, f"BAR increase proposed on Nova group/low-inventory nights: {bad}"


def hyd_peak_identified_and_capped(res):
    o = _out(res, HYD, "pricing_optimization")
    assert any(x["date"] == "2026-08-30" for x in o["identified_opportunities"]), "TechCon night 2026-08-30 not identified"
    for h, cur, new, d in _increases(res):
        if h == HYD and d == "2026-08-30":
            assert new <= cur * 115 // 100, f"increase to {new} exceeds +15% of {cur}"


def increases_respect_guardrails(res):
    ds = res["ds"]
    for h, cur, new, d in _increases(res):
        row = next(r for r in F.date_rows(ds, h) if r["date"] == d)
        assert row["bar"] == cur, f"{h} {d}: stated current BAR {cur} != data {row['bar']}"
        assert row["increase_allowed"], f"{h} {d}: increase on a date the rules block ({row['increase_block_reason']})"
        assert new <= row["max_bar_up"], f"{h} {d}: {new} > cap {row['max_bar_up']}"


def amber_no_mandatory_flag(res):
    o = _out(res, PNQ, "pricing_optimization")
    assert not any("Control Tower" in a["action"] for a in o["recommended_actions"]), \
        "Control Tower flag raised although the longest run below comp is < 7 days (rule 4.6)"
    assert any(f.category == "rule_vs_data" and f.hotel_id == PNQ and "4.6" in f.source for f in res["dq"]), "rule-vs-data conflict not reported"


def restrictions_only_on_high_demand(res):
    ds = res["ds"]
    for o in res["outputs"]:
        if o["agent"] != "channel_inventory_restriction":
            continue
        rows = {r["date"]: r for r in F.date_rows(ds, o["hotel_id"])}
        for a in o["recommended_actions"]:
            m = re.search(r"for (\d{4}-\d{2}-\d{2})", a["action"])
            if m:
                assert rows[m.group(1)]["high_demand"], f"{o['hotel_id']}: restriction on non-high-demand date {m.group(1)}"
                if a["action"].startswith("Set MinLOS"):
                    n = int(re.search(r"MinLOS(\d)", a["action"]).group(1))
                    ex = re.search(r"MinLOS(\d)", rows[m.group(1)]["restrictions"] or "")
                    assert not (ex and int(ex.group(1)) >= n), f"{o['hotel_id']}: repeats existing restriction"


def tasks_have_real_evidence(res):
    con = sqlite3.connect(C.DB_PATH)
    try:
        for t in res["tasks"]:
            assert t["evidence_refs"], f"{t['task_id']}: no evidence"
            for ref in t["evidence_refs"]:
                if "|" in ref:
                    ar, d = ref.split("|")
                    ok = con.execute("SELECT 1 FROM rate_pace WHERE ar_hotel_id=? AND stay_date=?", (ar, d)).fetchone()
                elif ref.startswith("BK-"):
                    ok = con.execute("SELECT 1 FROM bookings WHERE booking_id=?", (ref,)).fetchone()
                else:
                    ok = con.execute("SELECT 1 FROM hotels WHERE hotel_id=?", (ref,)).fetchone()
                assert ok, f"{t['task_id']}: evidence ref {ref} does not exist"
            for k in ("task_id", "hotel_id", "department", "title", "description", "priority", "owner_role", "created_at", "due_at", "status",
                      "source_agent", "evidence_refs", "requires_human_approval", "approved_by", "closure_note"):
                assert k in t, f"{t['task_id']}: missing section-9 field {k}"
            assert t["priority"] in ("P1", "P2", "P3", "P4") and t["status"] == "Open"
    finally:
        con.close()


def contract_adherence(res):
    for o in res["outputs"]:
        AgentOutput(**o)
        assert list(o.keys()) == ["agent", "hotel_id", "as_of", "summary", "identified_opportunities", "recommended_actions", "evidence",
                                  "confidence", "requires_human_approval", "data_gaps"]
        assert o["as_of"] == C.AS_OF_ISO
        for a in o["recommended_actions"]:
            assert set(a) == {"action", "owner_role", "due_at", "rationale"}
        if o["recommended_actions"]:
            assert o["requires_human_approval"] is True, "action without approval gate"
    assert len(res["outputs"]) == 8


def evidence_refs_exist(res):
    for o in res["outputs"]:
        f = F.build_facts(res["ds"], res["dq"], o["hotel_id"], o["agent"])
        allowed = F.allowed_refs(f)
        for e in o["evidence"]:
            assert e["ref"] in allowed[e["source"]], f"{o['hotel_id']}/{o['agent']}: evidence {e['source']}:{e['ref']} not in supplied data"


def no_guest_pii_in_outputs(res):
    from revintel.pii import find_pii

    blob = json.dumps([res["outputs"], res["tasks"], res["briefing"], res["audit"]], default=str)
    assert not find_pii(blob, res["ds"].raw_pii), f"raw guest PII leaked into outputs: {find_pii(blob, res['ds'].raw_pii)[:1]}"
    con = sqlite3.connect(C.DB_PATH)
    try:
        dump = "\n".join(con.iterdump())
    finally:
        con.close()
    hits = find_pii(dump, res["ds"].raw_pii)
    assert not hits, f"raw guest PII present in SQLite view: {hits[:1]}"


def low_confidence_where_data_is_bad(res):
    c = {(o["hotel_id"], o["agent"]): o["confidence"] for o in res["outputs"]}
    assert all(0 <= v <= 0.9 for v in c.values()), "confidence outside 0-0.9 (never certain)"
    assert c[(NOV, "pricing_optimization")] < c[(HYD, "pricing_optimization")] or c[(NOV, "pricing_optimization")] <= 0.65, \
        "Nova (PMS over capacity on 28 Aug) should be less confident than Hyderabad"


CASES = [
    Case("identity_resolves", "4/4 hotels resolve, no row lost or silently dropped", identity_resolves),
    Case("nova_no_increase_on_group_nights", "§4.3/§4.4: pickup driven by the group block + <10 rooms left must not trigger a BAR increase", nova_no_increase_on_group_nights),
    Case("hyd_peak_identified_and_capped", "TechCon night identified; any increase capped at +15% (§4.2)", hyd_peak_identified_and_capped),
    Case("increases_respect_guardrails", "Every increase anywhere: allowed by §4.4, within +15%, current BAR matches data", increases_respect_guardrails),
    Case("amber_no_mandatory_flag", "§4.6: 3-day run is not 7+ days; data-vs-rule conflict reported", amber_no_mandatory_flag),
    Case("restrictions_only_on_high_demand", "Restrictions only on high-demand dates, never repeating one already in place", restrictions_only_on_high_demand),
    Case("tasks_have_real_evidence", "§9: every task has evidence refs that exist, with all required fields", tasks_have_real_evidence),
    Case("contract_adherence", "Exact house contract on all 8 agent outputs", contract_adherence),
    Case("evidence_refs_exist", "No agent evidence entry cites a record that is not in the supplied data", evidence_refs_exist),
    Case("no_guest_pii_in_outputs", "No raw guest name/phone/email in outputs or the SQLite view", no_guest_pii_in_outputs),
    Case("low_confidence_where_data_is_bad", "Confidence is capped, and lower where data is unreliable", low_confidence_where_data_is_bad),
]
