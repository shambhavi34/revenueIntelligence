"""Post-LLM enforcement. The model proposes; this module disposes.

Nothing the model says is trusted. For every draft we:
  1. validate dates and evidence refs against the registry built from the facts it was given;
  2. re-check each action against business_rules_and_sops.md section 4 using code-computed numbers
     (clamp to +/-15%, block increases under 10 rooms without confirmed compression, closable plans only, ...);
  3. rewrite every price/number in an action's text from structured fields (so text and number cannot disagree);
  4. flag figures in the narrative that appear nowhere in the supplied data;
  5. cap confidence by data-quality warnings and by how much we had to intervene.

Each intervention is recorded (audit.json, shown in the UI) - silent fixes would defeat the point.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config as C
from . import facts as F
from . import tasks as T
from .contract import AgentOutput

PRIORITY_RANK = {"High": 0, "Medium": 1, "Low": 2}
ROLE = "Revenue Manager"  # section 7: "All rate and inventory actions"


class ContractError(ValueError):
    pass


@dataclass
class Processed:
    output: dict
    extras: list[dict]
    interventions: list[dict] = field(default_factory=list)


def _iv(kind, target, detail, effect, rule=None):
    return {"kind": kind, "rule": rule, "target": target, "detail": detail, "effect": effect}


def describe(source: str, ref: str, facts: dict) -> str:
    """Deterministic, grounded description of a cited record (used when the model's own detail is missing)."""
    if source == "rate_pace.csv":
        r = next((x for x in facts["dates"] if x["ref"] == ref), None)
        if r:
            s = (f"BAR INR {r['bar']} vs comp INR {r['comp_avg']} ({r['gap_vs_comp_pct']:+.1f}%); sold {r['sold']}/{r['capacity']} "
                 f"({r['occ_pct']}%), {r['remaining']} left; pickup_7d {r['pickup_7d']}; group rooms {r['group_rooms']}; OTA {r['ota_pct']}%")
            if r["event"]:
                s += f"; event: {r['event']}"
            if r["restrictions"]:
                s += f"; restrictions: {r['restrictions']}"
            return s
    if source == "bookings.csv":
        for g in facts["group_blocks"]:
            if ref in g["example_booking_ids"]:
                return f"Group block {g['code']} booking row (rate plan GROUP-CONTRACT, INR {g['adr']}, {g['arrive']} to {g['depart']})"
        for r in facts["dates"]:
            for b in r.get("lowest_adr_transient_bookings", []):
                if b[0] == ref:
                    return f"Transient booking on {r['date']}: plan {b[1]}, channel {b[2]}, ADR INR {b[3]}"
    if source == "business_rules_and_sops.md":
        return C.RULES.get(ref, ref)
    if source == "data_quality_report":
        w = next((w for w in facts["data_warnings"] if w["id"] == ref), None)
        return w["text"] if w else ref
    if source == "property_master.csv":
        h = facts["hotel"]
        return f"{h['name']} ({h['brand']}, {h['city']}), {h['capacity']} rooms"
    return ref


def _norm_ref(source: str, ref: str, ar_id: str) -> str:
    ref = str(ref).strip()
    if source == "rate_pace.csv" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ref):
        return f"{ar_id}|{ref}"
    if source == "business_rules_and_sops.md" and re.fullmatch(r"\d\.?\d*", ref):
        return "§" + ref
    return ref


def process_draft(agent: str, facts: dict, draft: dict) -> Processed:
    hotel_id = facts["hotel"]["hotel_id"]
    byd = {r["date"]: r for r in facts["dates"]}
    allowed = F.allowed_refs(facts)
    ar_id = facts["dates"][0]["ref"].split("|")[0]
    iv: list[dict] = []
    gaps = [str(g) for g in draft.get("data_gaps", [])]
    dropped_evidence = 0

    def check_ref(source, ref):
        return source in allowed and ref in allowed[source]

    # ---- evidence ----------------------------------------------------------------
    evidence, seen = [], set()
    for e in draft.get("evidence", []):
        src, ref = e.get("source"), _norm_ref(e.get("source", ""), e.get("ref", ""), ar_id)
        if not check_ref(src, ref):
            dropped_evidence += 1
            iv.append(_iv("unknown_evidence_ref", f"{src}:{ref}", "Cited record does not exist in the supplied data; evidence entry removed.", "dropped"))
            continue
        if (src, ref) not in seen:
            seen.add((src, ref))
            evidence.append({"source": src, "ref": ref, "detail": str(e.get("detail", "")).strip() or describe(src, ref, facts)})

    # ---- opportunities -----------------------------------------------------------
    opps = []
    for o in draft.get("identified_opportunities", []):
        if o.get("date") not in byd or o.get("priority") not in PRIORITY_RANK:
            iv.append(_iv("unknown_date", str(o.get("date")), "Opportunity date is not in the supplied horizon (or bad priority); removed.", "dropped"))
            continue
        opps.append({"date": o["date"], "priority": o["priority"], "why": str(o.get("why", "")).strip()})
    opps.sort(key=lambda o: (PRIORITY_RANK[o["priority"]], o["date"]))  # stable: model order kept within a tier

    # ---- actions -----------------------------------------------------------------
    extras: list[dict] = []
    proposed_prices: set[int] = set()
    for a in draft.get("recommended_actions", []):
        handler = _pricing_action if agent == "pricing_optimization" else _restriction_action
        extras += handler(a, facts, byd, iv, proposed_prices)

    # attach/validate evidence per action and merge into the global evidence list
    for e in extras:
        refs = []
        for r in e["evidence_refs"]:
            src, ref = r["source"], _norm_ref(r["source"], r["ref"], ar_id)
            if check_ref(src, ref):
                refs.append((src, ref))
            else:
                dropped_evidence += 1
                iv.append(_iv("unknown_evidence_ref", f"{src}:{ref}", f"Action '{e['action_text']}' cited a record that does not exist; removed.", "dropped"))
        data_refs = [x for x in refs if x[0] in ("rate_pace.csv", "bookings.csv", "property_master.csv")]
        if not data_refs:  # section 9: no data record -> auto-attach the real record the action is about
            if e.get("target_date") in byd:
                auto = [("rate_pace.csv", byd[e["target_date"]]["ref"])]
            elif facts["comp_set"]["refs_below_10pct"]:
                auto = [("rate_pace.csv", r) for r in facts["comp_set"]["refs_below_10pct"][:3]]
            else:
                auto = [("property_master.csv", hotel_id)]
            refs += auto
            iv.append(_iv("auto_attached_evidence", e["action_text"], "Model cited no data record; attached the rate_pace row the action targets.", "added"))
        if e.get("rule") and ("business_rules_and_sops.md", e["rule"]) not in refs:
            refs.append(("business_rules_and_sops.md", e["rule"]))
        e["evidence_refs"] = [{"source": s, "ref": r} for s, r in dict.fromkeys(refs)]
        e["evidence_task_refs"] = [r for s, r in dict.fromkeys(refs) if s in ("rate_pace.csv", "bookings.csv", "property_master.csv")]
        for s, r in dict.fromkeys(refs):
            if (s, r) not in seen:
                seen.add((s, r))
                evidence.append({"source": s, "ref": r, "detail": describe(s, r, facts)})

    # ---- numeric grounding -------------------------------------------------------
    allowed_nums = F.fact_numbers(facts) | proposed_prices | {e["new_bar"] for e in extras if e.get("new_bar")}
    texts = [draft.get("summary", "")] + [o["why"] for o in opps] + [e["rationale"] for e in extras] + gaps + \
            [e["action_text"] for e in extras if not e.get("generated_text")]
    bad = sorted({n for t in texts for n in F.numbers_in(t) if n not in allowed_nums and not 1900 < n < 2100})
    ungrounded = bool(bad)
    if bad:
        iv.append(_iv("ungrounded_numbers", ", ".join(map(str, bad)), "Figures in the agent narrative do not appear anywhere in the supplied data.", "flagged", "§8"))
        gaps.append(f"Narrative contains figure(s) not found in the supplied data: {', '.join(map(str, bad))} - treat as unverified.")
    if dropped_evidence:
        gaps.append(f"{dropped_evidence} cited evidence reference(s) did not exist in the supplied data and were removed.")

    # ---- confidence --------------------------------------------------------------
    warns = facts["data_warnings"]
    cap = 0.9 - 0.10 * sum(w["severity"] == "high" for w in warns) - 0.03 * sum(w["severity"] == "warn" for w in warns)
    cap = max(0.4, cap)
    try:
        conf = float(draft.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    penalty = min(0.2, 0.05 * dropped_evidence) + (0.15 if ungrounded else 0) + \
        min(0.2, 0.05 * sum(1 for i in iv if i["effect"] == "dropped" and i["kind"] not in ("unknown_evidence_ref",))) + \
        0.05 * sum(1 for i in iv if i["kind"] == "group_confounded_date")
    final_conf = round(max(0.1, min(conf, cap) - penalty), 2)
    if final_conf < conf:
        iv.append(_iv("confidence_capped", f"{conf:.2f} -> {final_conf:.2f}", f"Cap {cap:.2f} from {len(warns)} data warnings; penalty {penalty:.2f}.", "capped"))

    out = {
        "agent": agent, "hotel_id": hotel_id, "as_of": C.AS_OF_ISO,
        "summary": str(draft.get("summary", "")).strip(),
        "identified_opportunities": opps,
        "recommended_actions": [{"action": e["action_text"], "owner_role": e["owner_role"], "due_at": e["due_at"], "rationale": e["rationale"]} for e in extras],
        "evidence": evidence, "confidence": final_conf, "requires_human_approval": bool(extras),
        "data_gaps": list(dict.fromkeys(gaps)),
    }
    try:
        out = AgentOutput(**out).model_dump()
    except Exception as exc:  # pragma: no cover - surfaced loudly
        raise ContractError(str(exc)) from exc
    return Processed(out, extras, iv)


# ------------------------------------------------------------------------------------------
def _base_extra(a, facts, text, rationale, rule=None, generated=True, owner=ROLE, priority=None):
    urgency = a.get("urgency") if a.get("urgency") in PRIORITY_RANK else "Medium"
    pr = priority or T.priority_for(urgency)
    d = a.get("target_date")
    return {"action_text": text, "owner_role": owner, "rationale": rationale, "urgency": urgency, "priority": pr,
            "due_at": T.due_at(pr, d), "target_date": d, "title": text, "description": rationale,
            "evidence_refs": list(a.get("evidence_refs", [])), "rule": rule, "generated_text": generated, "type": a.get("action_type")}


def _pricing_action(a, facts, byd, iv, proposed_prices):
    t, d = a.get("action_type"), a.get("target_date")
    rationale = str(a.get("rationale", "")).strip()
    if t in ("increase_bar", "decrease_bar"):
        row = byd.get(d)
        if not row:
            iv.append(_iv("unknown_date", str(d), f"{t} targets a date outside the supplied horizon; removed.", "dropped"))
            return []
        cur = row["bar"]
        try:
            new = int(a.get("new_bar_inr"))
        except (TypeError, ValueError):
            iv.append(_iv("missing_price", d, f"{t} had no usable new_bar_inr; removed.", "dropped"))
            return []
        proposed_prices.add(new)
        notes = []
        exception = None
        if t == "increase_bar":
            if new <= cur:
                iv.append(_iv("not_an_increase", d, f"Proposed INR {new} <= current INR {cur}; removed.", "dropped"))
                return []
            if not row["increase_allowed"]:
                iv.append(_iv("increase_blocked", d, f"Increase to INR {new} blocked. {row['increase_block_reason']}", "dropped", "§4.4"))
                return []
            if row["compression_confirmed"] and row["remaining"] < C.MIN_REMAINING_FOR_INCREASE:
                notes.append(f"§4.4 exception applies: confirmed compression event ({row['event']}) with {row['remaining']} rooms left.")
            if not row["pickup_usable_as_transient_signal"]:
                iv.append(_iv("group_confounded_date", d, "Increase proposed on a date with a group block; pickup there is not transient demand evidence.", "annotated", "§4.3"))
                notes.append("§4.3: pickup on this date is group-confounded; do not treat it as transient demand.")
            cap = row["max_bar_up"]
            if new > cap:
                iv.append(_iv("clamped_to_15pct", d, f"Proposed INR {new} exceeds +15% cap; capped at INR {cap}.", "clamped", "§4.2"))
                notes.append(f"Guardrail §4.2: proposed INR {new} exceeds the +15% cap; capped at INR {cap}. A move to INR {new} needs Operations Head sign-off.")
                exception = (new, cap)
                new = cap
            text = f"Increase BAR from INR {cur} to INR {new} for {d}"
        else:
            if new >= cur:
                iv.append(_iv("not_a_decrease", d, f"Proposed INR {new} >= current INR {cur}; removed.", "dropped"))
                return []
            cap = row["max_bar_down"]
            if new < cap:
                iv.append(_iv("clamped_to_15pct", d, f"Proposed INR {new} exceeds -15% cap; floored at INR {cap}.", "clamped", "§4.2"))
                notes.append(f"Guardrail §4.2: proposed INR {new} exceeds the -15% cap; floored at INR {cap}. A move to INR {new} needs Operations Head sign-off.")
                exception = (new, cap)
                new = cap
            text = f"Decrease BAR from INR {cur} to INR {new} for {d}"
        e = _base_extra(a, facts, text, " ".join([rationale] + notes).strip(), "§4.1")
        e["new_bar"], e["current_bar"] = new, cur
        e["evidence_refs"] += [{"source": "rate_pace.csv", "ref": row["ref"]}]
        out = [e]
        if exception:
            prop, capped = exception
            pct = round((prop - cur) / cur * 100, 1)
            x = _base_extra(a, facts, f"Operations Head sign-off needed to move BAR to INR {prop} ({pct:+.1f}%) for {d}",
                            f"Revenue Manager can approve up to INR {capped} (±15%). Going to INR {prop} is outside the §4.2 band and is an exception requiring Operations Head sign-off.",
                            "§4.2", owner="Operations Head")
            x["pipeline_derived"] = True
            x["new_bar"] = None
            x["evidence_refs"] = [{"source": "rate_pace.csv", "ref": row["ref"]}]
            out.append(x)
        return out

    if t == "flag_comp_set_control_tower":
        runs = facts["comp_set"]
        if runs["longest_run_below_10pct"] >= C.COMP_RUN_DAYS:
            text = (f"Flag {facts['hotel']['name']} to Revenue Control Tower: BAR >10% below comp-set average for "
                    f"{runs['longest_run_below_10pct']} consecutive days")
            e = _base_extra(a, facts, text, rationale, "§4.6")
            e["evidence_refs"] += [{"source": "rate_pace.csv", "ref": r} for r in runs["refs_below_10pct"][:5]]
            return [e]
        iv.append(_iv("flag_not_triggered", hotel_label(facts), f"Longest run >10% below comp is {runs['longest_run_below_10pct']} days (<{C.COMP_RUN_DAYS}); converted to a pricing review.", "converted", "§4.6"))
        a = dict(a, action_type="review_pricing_position", action="")  # drop the model's "flag" wording: the flag was not earned
        t = "review_pricing_position"
        rationale = (rationale + f" Note (§4.6): the mandatory Control Tower flag is not triggered - longest run >10% below comp is "
                     f"{runs['longest_run_below_10pct']} consecutive days of the {runs['window_days']}-day window (rule needs {C.COMP_RUN_DAYS}+).").strip()

    if t == "review_pricing_position":
        d = d if d in byd else None
        a = dict(a, target_date=d)
        text = str(a.get("action", "")).strip()[:200] or f"Review BAR position vs comp set for {facts['hotel']['name']}"
        e = _base_extra(a, facts, text, rationale, "§4.6", generated=False)
        if not d:
            e["evidence_refs"] += [{"source": "rate_pace.csv", "ref": r} for r in facts["comp_set"]["refs_below_10pct"][:5]]
        return [e]
    iv.append(_iv("unknown_action_type", str(t), "Action type not recognised; removed.", "dropped"))
    return []


def hotel_label(facts):
    return facts["hotel"]["hotel_id"]


def _restriction_action(a, facts, byd, iv, proposed_prices):
    t, d = a.get("action_type"), a.get("target_date")
    rationale = str(a.get("rationale", "")).strip()
    if t == "review_channel_mix":
        d = d if d in byd else None
        a = dict(a, target_date=d)
        text = str(a.get("action", "")).strip()[:200] or f"Review channel mix for {facts['hotel']['name']}"
        return [_base_extra(a, facts, text, rationale, None, generated=False)]
    row = byd.get(d)
    if t not in ("set_minlos", "close_rate_plan"):
        iv.append(_iv("unknown_action_type", str(t), "Action type not recognised; removed.", "dropped"))
        return []
    if not row:
        iv.append(_iv("unknown_date", str(d), f"{t} targets a date outside the supplied horizon; removed.", "dropped"))
        return []
    if not row["high_demand"]:
        iv.append(_iv("not_high_demand", d, f"{t} on a date that is not high-demand (occupancy {row['occ_pct']}%, no event, no group); removed.", "dropped"))
        return []
    ev = [{"source": "rate_pace.csv", "ref": row["ref"]}]
    if t == "set_minlos":
        try:
            n = int(a.get("min_los"))
        except (TypeError, ValueError):
            n = 0
        if not 2 <= n <= 5:
            iv.append(_iv("bad_minlos", d, f"min_los={a.get('min_los')} outside 2-5; removed.", "dropped"))
            return []
        m = re.search(r"MinLOS\s*(\d)", row["restrictions"] or "", re.I)
        if m and int(m.group(1)) >= n:
            iv.append(_iv("already_in_place", d, f"MinLOS{m.group(1)} already set on this date ({row['restrictions']}); removed.", "dropped"))
            return []
        text = f"Set MinLOS{n} on OTA channels for {d}"
        e = _base_extra(a, facts, text, rationale)
        e["evidence_refs"] += ev
        return [e]
    plan = a.get("rate_plan")
    if plan not in C.DISCOUNT_PLANS:
        iv.append(_iv("plan_not_closable", d, f"Plan '{plan}' is not a closable discount plan {sorted(C.DISCOUNT_PLANS)}; removed.", "dropped"))
        return []
    notes, rule = [], None
    if row["ota_pct"] > C.OTA_VISIBILITY_THRESHOLD:
        notes.append(f"Visibility risk (§4.5): channel_mix_ota_pct is {row['ota_pct']}% (>{C.OTA_VISIBILITY_THRESHOLD}%); closing a plan on OTAs may reduce ranking and conversion.")
        iv.append(_iv("visibility_risk_flag", d, f"OTA mix {row['ota_pct']}% > 60%; visibility-risk note added.", "annotated", "§4.5"))
        rule = "§4.5"
    text = f"Close {plan} on OTA channels for {d}"
    e = _base_extra(a, facts, text, " ".join([rationale] + notes).strip(), rule)
    e["evidence_refs"] += ev
    return [e]
