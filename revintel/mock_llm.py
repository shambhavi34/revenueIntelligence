"""MockLLM - a heuristic STAND-IN for the judgment layer. It is not an LLM.

Why it exists: so the whole pipeline, UI and tests run on a clean machine with no API key and no network.
Every output it produces carries provider="mock-heuristic" and the UI shows a banner.

It deliberately makes the same *tempting* proposals a naive model might (price to full comp parity; a Control
Tower flag because the notes say "consistently below comp"; repeating a restriction already in place) so that the
post-check in guardrails.py has something real to catch in demos and tests. Real runs use AnthropicLLM.
"""
from __future__ import annotations

import json

from .llm import LLMResult, prompt_sha


def _facts(user: str) -> dict:
    return json.loads(user.split("FACTS:\n", 1)[1])


def _ev(r):
    return {"source": "rate_pace.csv", "ref": r["ref"]}


class MockLLM:
    name = "mock-heuristic"
    model = "mock"

    def draft(self, agent: str, system: str, user: str, schema: dict) -> LLMResult:
        facts = _facts(user)
        d = self._pricing(facts) if agent == "pricing_optimization" else self._restriction(facts)
        return LLMResult(d, self.name, self.model, prompt_sha(self.model, system, user, schema))

    # ------------------------------------------------------------------------------------------
    def _pricing(self, f):
        rows, opps, acts, gaps = f["dates"], [], [], []
        for r in rows:
            if r["compression_confirmed"] and r["gap_vs_comp_pct"] < -5:
                opps.append({"date": r["date"], "priority": "High", "why":
                             f"{r['event']}: occupancy {r['occ_pct']}% with pickup {r['pickup_7d']} and no group block, yet BAR INR {r['bar']} is "
                             f"{abs(r['gap_vs_comp_pct'])}% below comp INR {r['comp_avg']}; only {r['remaining']} rooms left."})
                acts.append({"action_type": "increase_bar", "new_bar_inr": r["comp_avg"], "target_date": r["date"],
                             "action": "Raise BAR to comp-set parity", "urgency": "High",
                             "rationale": f"Confirmed compression (event flag, notes, accelerating pickup) and BAR INR {r['bar']} well under comp INR {r['comp_avg']}.",
                             "evidence_refs": [_ev(r), {"source": "business_rules_and_sops.md", "ref": "§4.4"}]})
            elif r["group_rooms"] and r["pickup_7d"] >= 10:
                opps.append({"date": r["date"], "priority": "High", "why":
                             f"Occupancy {r['occ_pct']}% looks hot, but the pickup of {r['pickup_7d']} sits on a {r['group_rooms']}-room group block and only "
                             f"{r['remaining']} rooms remain: no BAR increase."})
            elif r["occ_pct"] >= 80 and not r["increase_allowed"]:
                opps.append({"date": r["date"], "priority": "Low", "why":
                             f"Occupancy {r['occ_pct']}% but only {r['remaining']} rooms left and no confirmed compression: hold BAR at INR {r['bar']}."})
            elif r["group_rooms"] and r["occ_pct"] >= 80:
                opps.append({"date": r["date"], "priority": "Medium", "why":
                             f"Group block nights ({r['group_rooms']} rooms): {r['remaining']} rooms left, hold BAR."})
            elif r["occ_pct"] < 50 and r["pickup_7d"] > 0:
                opps.append({"date": r["date"], "priority": "Medium", "why":
                             f"Occupancy only {r['occ_pct']}% on a night after the group block, BAR INR {r['bar']} vs comp INR {r['comp_avg']}; watch pickup ({r['pickup_7d']})."})
        cs = f["comp_set"]
        if len(cs["dates_below_10pct"]) >= 3:
            consistently = any("consistently" in (r["notes"] or "").lower() for r in rows)
            kind = "flag_comp_set_control_tower" if consistently else "review_pricing_position"
            opps.append({"date": cs["dates_below_10pct"][0], "priority": "High", "why":
                         f"BAR is more than 10% below comp on {len(cs['dates_below_10pct'])} of {cs['window_days']} dates while occupancy is only "
                         f"{min(r['occ_pct'] for r in rows)}-{max(r['occ_pct'] for r in rows)}%."})
            acts.append({"action_type": kind, "new_bar_inr": None, "target_date": None, "action": "Review BAR position vs comp set",
                         "urgency": "Medium", "rationale": "Revenue notes say BAR is consistently below comp and the owner has raised it.",
                         "evidence_refs": [_ev(r) for r in rows if r["date"] in cs["dates_below_10pct"][:3]]})
        gaps += [w["text"] for w in f["data_warnings"] if "forecast_rooms" in w["text"] or "out-of-order" in w["text"]][:2]
        if not opps:
            opps = []
        top = opps[0]["why"] if opps else "No date shows a clear BAR action under the guardrails."
        return {"summary": f"{f['hotel']['name']}: {len(acts)} proposed BAR action(s). {top}",
                "identified_opportunities": opps, "recommended_actions": acts,
                "evidence": [_ev(r) for r in rows if any(r["date"] == o["date"] for o in opps)][:6],
                "confidence": 0.7, "data_gaps": gaps}

    def _restriction(self, f):
        rows, opps, acts, gaps = f["dates"], [], [], []
        hot = [r for r in rows if r["high_demand"]]
        hot.sort(key=lambda r: (-(bool(r["event"]) or bool(r["group_rooms"])), -r["occ_pct"]))
        for r in hot[:3]:
            why = (f"Occupancy {r['occ_pct']}% with {r['remaining']} rooms left"
                   + (f" ({r['event']})" if r["event"] else "") + (f", {r['group_rooms']}-room group block" if r["group_rooms"] else "")
                   + f"; OTA mix {r['ota_pct']}%"
                   + (f"; existing restriction: {r['restrictions']}" if r["restrictions"] else "; no restriction set") + ".")
            opps.append({"date": r["date"], "priority": "High" if (r["event"] or r["group_rooms"]) else "Medium", "why": why})
            acts.append({"action_type": "set_minlos", "min_los": 2, "rate_plan": None, "target_date": r["date"], "action": "MinLOS2 on OTA",
                         "urgency": "High" if (r["event"] or r["group_rooms"]) else "Medium",
                         "rationale": "Protect multi-night stays on a constrained date.", "evidence_refs": [_ev(r)]})
        if hot:
            r = hot[0]
            low = r.get("lowest_adr_transient_bookings", [])
            acts.append({"action_type": "close_rate_plan", "min_los": None, "rate_plan": "OTA-PROMO", "target_date": r["date"],
                         "action": "Close OTA promo", "urgency": "High" if (r["event"] or r["group_rooms"]) else "Medium",
                         "rationale": "Discounted OTA promo inventory dilutes ADR on the tightest date."
                                      + (f" Lowest-ADR transient bookings include {low[0][0]} at INR {low[0][3]}." if low else ""),
                         "evidence_refs": [_ev(r)] + ([{"source": "bookings.csv", "ref": low[0][0]}] if low else [])})
        else:
            hi = [r for r in rows if r["ota_pct"] > 60]
            if hi:
                acts.append({"action_type": "review_channel_mix", "min_los": None, "rate_plan": None, "target_date": None,
                             "action": "Review OTA dependence", "urgency": "Low",
                             "rationale": f"OTA mix exceeds 60% on {len(hi)} of {len(rows)} dates while occupancy stays below 70%; no date is high-demand so no restriction is warranted.",
                             "evidence_refs": [_ev(r) for r in hi[:3]]})
        gaps += [w["text"] for w in f["data_warnings"] if "channel_mix_ota_pct" in w["text"]][:1]
        top = opps[0]["why"] if opps else "No high-demand date in the window: no restriction recommended."
        return {"summary": f"{f['hotel']['name']}: {len(acts)} proposed restriction action(s). {top}",
                "identified_opportunities": opps, "recommended_actions": acts,
                "evidence": [_ev(r) for r in hot[:3]], "confidence": 0.7, "data_gaps": gaps}
