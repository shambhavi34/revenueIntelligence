"""Action layer: recommendation -> accountable task (business_rules_and_sops.md section 9).

Task fields: task_id, hotel_id, department, title, description, priority (P1..P4), owner_role, created_at,
due_at (derived from SLA), status, source_agent, evidence_refs[], requires_human_approval, approved_by, closure_note.

Assumptions (no revenue SLA exists in the rules; documented in README):
* urgency High/Medium/Low -> P2/P3/P4 (P1 is reserved for safety / guest-critical issues in section 2).
* due_at = as_of + the section-2 resolution SLA for that priority (P2 12h, P3 24h, P4 48h), but never later than
  18:00 IST the day before the stay date being acted on, and never sooner than as_of + 2h.
* A task is only created if it carries at least one real evidence ref (section 9: otherwise it is rejected).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from . import config as C

ABBR = {"pricing_optimization": "PRC", "channel_inventory_restriction": "RST", "data_quality": "DQ"}


def priority_for(urgency: str) -> str:
    return C.PRIORITY_FROM_URGENCY[urgency]


def due_at(priority: str, target_date: str | None) -> str:
    due = C.AS_OF + timedelta(hours=C.SLA_HOURS[priority])
    if target_date:
        cutoff = datetime.combine(date.fromisoformat(target_date) - timedelta(days=1), time(18, 0), tzinfo=C.IST)
        due = min(due, cutoff)
    due = max(due, C.AS_OF + timedelta(hours=C.MIN_LEAD_HOURS))
    return due.isoformat()


def build_tasks(hotel_id: str, agent: str, extras: list[dict]) -> tuple[list[dict], list[dict]]:
    """extras: per-action records from guardrails.process_draft. Returns (tasks, rejected)."""
    tasks, rejected = [], []
    n = 0
    for e in extras:
        refs = [r for r in e["evidence_task_refs"]]
        if not refs:
            rejected.append({"action": e["action_text"], "reason": "no evidence ref (section 9)"})
            continue
        n += 1
        pr = e["priority"]
        tasks.append({
            "task_id": f"TSK-{C.AS_OF:%Y%m%d}-{hotel_id}-{ABBR[agent]}-{n:02d}",
            "hotel_id": hotel_id, "department": "Revenue",
            "title": e["title"], "description": e["description"],
            "priority": pr, "owner_role": e["owner_role"],
            "created_at": C.AS_OF_ISO, "due_at": e["due_at"], "status": "Open",
            "source_agent": agent if not e.get("pipeline_derived") else f"{agent} (pipeline-derived)",
            "evidence_refs": refs, "requires_human_approval": True,
            "approved_by": None, "closure_note": None,
            "target_date": e.get("target_date"),
        })
    return tasks, rejected


def dq_tasks(ds_findings) -> list[dict]:
    """Deterministic (non-LLM) tasks for data problems that make inventory numbers unreliable."""
    tasks = []
    n = {}
    for f in ds_findings:
        if f.category != "over_capacity":
            continue
        n[f.hotel_id] = n.get(f.hotel_id, 0) + 1
        refs = [r for r in f.refs if not r.startswith("...")]
        pr = "P2"
        d = re.search(r"\d{4}-\d{2}-\d{2}", f.summary).group(0)
        tasks.append({
            "task_id": f"TSK-{C.AS_OF:%Y%m%d}-{f.hotel_id}-DQ-{n[f.hotel_id]:02d}",
            "hotel_id": f.hotel_id, "department": "Revenue",
            "title": f"Reconcile PMS vs AxisRooms inventory for {d}",
            "description": f"{f.summary}. Confirm true room count before any rate or restriction change on this date. "
                           f"({f.id})",
            "priority": pr, "owner_role": "Revenue Manager", "created_at": C.AS_OF_ISO, "due_at": due_at(pr, d),
            "status": "Open", "source_agent": "data_quality", "evidence_refs": refs, "requires_human_approval": False,
            "approved_by": None, "closure_note": None, "target_date": d,
        })
    return tasks
