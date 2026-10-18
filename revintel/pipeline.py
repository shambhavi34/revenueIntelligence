"""End-to-end run:  python -m revintel.pipeline [--provider auto|anthropic|replay|mock] [--refresh]

ingest -> DQ report -> facts -> 2 agents x N hotels -> post-check -> tasks -> outputs/*.json + SQLite.
Nothing here writes to a PMS or channel manager: the output is a briefing and a task list for humans.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter

import pandas as pd

from . import config as C
from . import facts as F
from . import ingest, tasks as T
from .agents import AGENTS
from .llm import get_llm

PRIO = {"High": 0, "Medium": 1, "Low": 2}


def render_dq_markdown(ds, findings) -> str:
    c = Counter(f.severity for f in findings)
    lines = ["# Data quality report", "",
             f"Briefing time: {C.AS_OF_ISO}. Sources: property_master.csv ({len(ds.master)} hotels), bookings.csv "
             f"({len(ds.bookings) + len(ds.bookings_quarantine)} rows), rate_pace.csv ({len(ds.rate) + len(ds.rate_quarantine)} rows).", "",
             f"**{c['high']} high · {c['warn']} warn · {c['info']} info.** "
             f"Unresolved/quarantined: {len(ds.bookings_quarantine)} booking rows, {len(ds.rate_quarantine)} rate rows. "
             "No row was dropped silently; nothing was joined by guesswork.", ""]
    for sev, title in (("high", "High"), ("warn", "Warnings"), ("info", "Information & assumptions")):
        fs = [f for f in findings if f.severity == sev]
        if not fs:
            continue
        lines += [f"## {title}", ""]
        for f in fs:
            lines += [f"### {f.id} · {f.category}" + (f" · {f.hotel_id}" if f.hotel_id else ""),
                      f"- **Found:** {f.summary}", f"- **Source:** {f.source}", f"- **What we did:** {f.action_taken}"]
            if f.refs:
                lines.append(f"- **Records:** {', '.join(f.refs[:12])}")
            lines.append("")
    return "\n".join(lines)


def run(provider: str = "auto", refresh: bool = False, hotels: list[str] | None = None, write: bool = True) -> dict:
    llm = get_llm(provider, refresh=refresh)
    ds = ingest.build(write_db=False)
    ds.findings += F.signal_findings(ds)
    ds.findings = ingest.finalise_findings(ds.findings)
    findings = ds.findings

    hotel_ids = hotels or ds.hotel_ids()
    briefing_hotels, all_outputs, all_tasks, audits, facts_dump = [], [], [], [], {}
    for hid in hotel_ids:
        h = ds.master[ds.master.hotel_id == hid].iloc[0]
        agents_out, h_tasks, h_opps = {}, [], []
        for ag in AGENTS:
            r = ag.run(ds, findings, hid, llm)
            agents_out[ag.name] = r.output
            all_outputs.append(r.output)
            h_tasks += r.tasks
            audits.append(r.audit)
            facts_dump[f"{ag.name}__{hid}"] = r.facts
            for o in r.output["identified_opportunities"]:
                h_opps.append({"hotel_id": hid, "agent": ag.name, **o})
        h_tasks += [t for t in T.dq_tasks(findings) if t["hotel_id"] == hid]
        h_opps.sort(key=lambda o: (PRIO[o["priority"]], o["date"], o["agent"]))
        h_tasks.sort(key=lambda t: (t["priority"], t["due_at"], t["task_id"]))
        all_tasks += h_tasks
        briefing_hotels.append({"hotel_id": hid, "name": h.hotel_name, "brand": h.brand, "city": h.city, "capacity": int(h.rooms),
                                "agents": agents_out, "ranked_opportunities": h_opps, "tasks": h_tasks})

    top = sorted(all_tasks, key=lambda t: (t["priority"], t["due_at"], t["task_id"]))[:6]
    providers = Counter(a["provider"] or "failed" for a in audits)
    briefing = {"as_of": C.AS_OF_ISO, "providers": dict(providers), "models": sorted({a["model"] for a in audits if a["model"]}),
                "hotels": briefing_hotels, "top_task_ids": [t["task_id"] for t in top]}

    if write:
        C.OUT_DIR.mkdir(parents=True, exist_ok=True)
        (C.OUT_DIR / "facts").mkdir(exist_ok=True)
        dump = lambda name, obj: (C.OUT_DIR / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
        dump("briefing.json", briefing)
        dump("agent_outputs.json", all_outputs)  # exactly the house contract, nothing else
        dump("tasks.json", all_tasks)
        dump("audit.json", audits)
        dump("dq_report.json", [f.to_dict() for f in findings])
        (C.OUT_DIR / "dq_report.md").write_text(render_dq_markdown(ds, findings))
        for k, v in facts_dump.items():
            (C.OUT_DIR / "facts" / f"{k}.json").write_text(json.dumps(v, indent=1, ensure_ascii=False))
        ingest.write_sqlite(ds)
        con = sqlite3.connect(C.DB_PATH)
        pd.DataFrame([{**t, "evidence_refs": json.dumps(t["evidence_refs"])} for t in all_tasks]).to_sql("tasks", con, index=False, if_exists="replace")
        con.close()
    return {"briefing": briefing, "outputs": all_outputs, "tasks": all_tasks, "audit": audits, "dq": findings, "ds": ds}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate the Revenue Intelligence briefing for 25 Aug 2026 09:00 IST")
    ap.add_argument("--provider", default="auto", choices=["auto", "anthropic", "openai", "replay", "mock"])
    ap.add_argument("--refresh", action="store_true", help="ignore the on-disk LLM cache and call the model again")
    a = ap.parse_args(argv)
    res = run(a.provider, a.refresh)
    b = res["briefing"]
    print(f"as_of {b['as_of']} | providers {b['providers']} | models {b['models']}")
    for h in b["hotels"]:
        print(f"\n{h['hotel_id']}  {h['name']}")
        for t in h["tasks"]:
            print(f"  [{t['priority']}] due {t['due_at'][5:16]}  {t['owner_role']:<16} {t['title']}")
    print(f"\nWrote {C.OUT_DIR}/ (briefing.json, agent_outputs.json, tasks.json, audit.json, dq_report.md/json, facts/, revintel.db)")
    if "mock-heuristic" in b["providers"]:
        print("NOTE: judgment layer ran with the MOCK stand-in (no ANTHROPIC_API_KEY and no cached LLM responses).")


if __name__ == "__main__":
    main()
