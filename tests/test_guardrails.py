"""Adversarial drafts: what a careless or hallucinating model might say. The post-check must catch every one."""
from conftest import BLR, HYD, NOV, PNQ, draft, price_action, restr_action

from revintel.contract import AgentOutput
from revintel.guardrails import process_draft

PR, RS = "pricing_optimization", "channel_inventory_restriction"


def kinds(p):
    return [i["kind"] for i in p.interventions]


def test_increase_beyond_15pct_is_clamped_and_needs_ops_head(facts):
    p = process_draft(PR, facts(HYD), draft([price_action("2026-08-30", 5600)]))
    a = p.output["recommended_actions"]
    assert a[0]["action"] == "Increase BAR from INR 4300 to INR 4945 for 2026-08-30"   # 4300 * 1.15, integer math
    assert a[1]["owner_role"] == "Operations Head" and "5600" in a[1]["action"]       # exception task for the full move
    assert "clamped_to_15pct" in kinds(p)
    assert all(x["owner_role"] in ("Revenue Manager", "Operations Head") for x in a)


def test_increase_blocked_when_under_10_rooms_without_compression(facts):
    for hotel, date, price in [(NOV, "2026-08-28", 6500), (NOV, "2026-08-27", 6300), (BLR, "2026-08-26", 5400)]:
        p = process_draft(PR, facts(hotel), draft([price_action(date, price)]))
        assert p.output["recommended_actions"] == [], (hotel, date)
        assert "increase_blocked" in kinds(p)
        assert p.output["requires_human_approval"] is False


def test_guardrail_does_not_over_block(facts):
    p = process_draft(PR, facts(PNQ), draft([price_action("2026-08-25", 3500)]))   # 13 rooms left
    assert [a["action"] for a in p.output["recommended_actions"]] == ["Increase BAR from INR 3400 to INR 3500 for 2026-08-25"]
    assert p.output["requires_human_approval"] is True


def test_decrease_floor_and_direction(facts):
    p = process_draft(PR, facts(PNQ), draft([price_action("2026-08-25", 2000, typ="decrease_bar"),
                                              price_action("2026-08-26", 3300, typ="decrease_bar")]))
    texts = [a["action"] for a in p.output["recommended_actions"] if a["owner_role"] == "Revenue Manager"]
    assert "Decrease BAR from INR 3400 to INR 2890 for 2026-08-25" in texts   # floor = 85% of 3400
    p2 = process_draft(PR, facts(PNQ), draft([price_action("2026-08-26", 3500, typ="decrease_bar")]))
    assert p2.output["recommended_actions"] == [] and "not_a_decrease" in kinds(p2)


def test_action_text_is_rebuilt_from_structured_fields(facts):
    a = price_action("2026-08-30", 4900)
    a["action"] = "Raise BAR to INR 5100 (matching comp!)"   # model text disagrees with its own number
    p = process_draft(PR, facts(HYD), draft([a]))
    assert p.output["recommended_actions"][0]["action"] == "Increase BAR from INR 4300 to INR 4900 for 2026-08-30"


def test_invented_evidence_is_removed_and_confidence_drops(facts):
    ev = [{"source": "bookings.csv", "ref": "BK-999999", "detail": "made up"},
          {"source": "rate_pace.csv", "ref": "AR11103|2030-01-01", "detail": "made up"},
          {"source": "rate_pace.csv", "ref": "AR11103|2026-08-30", "detail": "real"}]
    clean = process_draft(PR, facts(HYD), draft(evidence=ev[2:]))
    dirty = process_draft(PR, facts(HYD), draft(evidence=ev))
    assert [e["ref"] for e in dirty.output["evidence"]] == ["AR11103|2026-08-30"]
    assert any("did not exist" in g for g in dirty.output["data_gaps"])
    assert dirty.output["confidence"] < clean.output["confidence"]


def test_date_only_rate_ref_is_normalised(facts):
    p = process_draft(PR, facts(HYD), draft(evidence=[{"source": "rate_pace.csv", "ref": "2026-08-30", "detail": "x"}]))
    assert p.output["evidence"][0]["ref"] == "AR11103|2026-08-30"


def test_unknown_dates_are_dropped(facts):
    p = process_draft(PR, facts(HYD), draft([price_action("2026-12-25", 5000)],
                                            opps=[{"date": "2026-12-25", "priority": "High", "why": "x"}]))
    assert p.output["recommended_actions"] == [] and p.output["identified_opportunities"] == []


def test_comp_flag_needs_seven_consecutive_days(facts):
    flag = {"action_type": "flag_comp_set_control_tower", "new_bar_inr": None, "target_date": None, "action": "Flag to Control Tower",
            "urgency": "High", "rationale": "notes say consistently below comp", "evidence_refs": []}
    p = process_draft(PR, facts(PNQ), draft([flag]))
    assert "Control Tower" not in p.output["recommended_actions"][0]["action"]
    assert "flag_not_triggered" in kinds(p) and "§4.6" in p.output["recommended_actions"][0]["rationale"]
    f = facts(PNQ)
    f["comp_set"]["longest_run_below_10pct"] = 7           # same model output, but the rule is met
    p2 = process_draft(PR, f, draft([flag]))
    assert "Control Tower" in p2.output["recommended_actions"][0]["action"]


def test_ungrounded_figures_are_flagged(facts):
    p = process_draft(PR, facts(HYD), draft(summary="Competitor rates will hit INR 9999 and 777 rooms are free."))
    assert "ungrounded_numbers" in kinds(p)
    assert any("9999" in g for g in p.output["data_gaps"])
    ok = process_draft(PR, facts(HYD), draft(summary="BAR INR 4300 vs comp INR 5100 with 9 rooms left."))
    assert "ungrounded_numbers" not in kinds(ok)


def test_confidence_is_capped_by_data_quality(facts):
    p = process_draft(PR, facts(NOV), draft(confidence=1.0))
    assert p.output["confidence"] <= 0.62            # Nova: PMS over capacity (high) + several warnings
    assert process_draft(PR, facts(HYD), draft(confidence=1.0)).output["confidence"] <= 0.9


def test_action_without_data_evidence_gets_the_real_record_attached(facts):
    p = process_draft(PR, facts(HYD), draft([price_action("2026-08-30", 4500, refs=[{"source": "business_rules_and_sops.md", "ref": "§4.4"}])]))
    assert "AR11103|2026-08-30" in p.extras[0]["evidence_task_refs"]


def test_contract_shape_is_enforced(facts):
    out = process_draft(PR, facts(HYD), draft()).output
    AgentOutput(**out)
    assert set(out) == {"agent", "hotel_id", "as_of", "summary", "identified_opportunities", "recommended_actions", "evidence",
                        "confidence", "requires_human_approval", "data_gaps"}


# ----- restriction agent --------------------------------------------------------------------
def test_restriction_rules(facts):
    f = facts(NOV, RS)
    p = process_draft(RS, f, draft([restr_action("set_minlos", "2026-08-28", min_los=2)]))          # MinLOS2 already on
    assert p.output["recommended_actions"] == [] and "already_in_place" in kinds(p)
    p = process_draft(RS, f, draft([restr_action("set_minlos", "2026-08-27", min_los=9),            # nonsense LOS
                                    restr_action("close_rate_plan", "2026-08-28", rate_plan="BAR-RO"),   # not a discount plan
                                    restr_action("close_rate_plan", "2026-08-28", rate_plan="GROUP-CONTRACT")]))
    assert p.output["recommended_actions"] == []
    assert set(kinds(p)) >= {"bad_minlos", "plan_not_closable"}
    p = process_draft(RS, facts(PNQ, RS), draft([restr_action("set_minlos", "2026-08-31", min_los=2)]))   # 55.6% occupancy, no event
    assert p.output["recommended_actions"] == [] and "not_high_demand" in kinds(p)
    p = process_draft(RS, f, draft([restr_action("set_minlos", "2026-08-27", min_los=2)]))
    assert p.output["recommended_actions"][0]["action"] == "Set MinLOS2 on OTA channels for 2026-08-27"


def test_closing_a_plan_above_60pct_ota_carries_a_visibility_flag(facts):
    f = facts(HYD, RS)
    row = next(r for r in f["dates"] if r["date"] == "2026-08-30")
    row["ota_pct"] = 71
    p = process_draft(RS, f, draft([restr_action("close_rate_plan", "2026-08-30", rate_plan="OTA-PROMO")]))
    r = p.output["recommended_actions"][0]["rationale"]
    assert "Visibility risk" in r and "§4.5" in r and "visibility_risk_flag" in kinds(p)
    f2 = facts(HYD, RS)
    p2 = process_draft(RS, f2, draft([restr_action("close_rate_plan", "2026-08-30", rate_plan="OTA-PROMO")]))   # 47% OTA: no flag needed
    assert "Visibility risk" not in p2.output["recommended_actions"][0]["rationale"]
