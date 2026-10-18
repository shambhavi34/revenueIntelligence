"""Revenue Intelligence Briefing - Streamlit UI.   streamlit run app.py

Reads the files the pipeline wrote to outputs/ (runs the pipeline on first launch if they are missing).
Everything the Revenue Manager needs to *check the machine's work* is one click from each recommendation:
the cited records are resolved from the unified SQLite view, not from the model's own words.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st

from revintel import config as C
from revintel import pipeline

st.set_page_config(page_title="Revenue Intelligence Briefing", page_icon="📈", layout="wide")

OUT = C.OUT_DIR
PRIO_ICON = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "⚪", "High": "🟠", "Medium": "🟡", "Low": "⚪"}
AGENT_LABEL = {"pricing_optimization": "Pricing Optimization", "channel_inventory_restriction": "Channel & Inventory Restriction"}


# --------------------------------------------------------------------------- data
def ensure_outputs():
    if not (OUT / "briefing.json").exists():
        with st.spinner("First launch: generating the briefing (data layer + agents)…"):
            pipeline.run("auto")


@st.cache_data(show_spinner=False)
def load(_stamp: float):
    rd = lambda n: json.loads((OUT / n).read_text())
    return {"briefing": rd("briefing.json"), "audit": rd("audit.json"), "dq": rd("dq_report.json"),
            "dq_md": (OUT / "dq_report.md").read_text()}


def db():
    return sqlite3.connect(C.DB_PATH)


def fmt_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a %d %b, %H:%M IST")


def resolve(ref: str) -> dict:
    """Resolve a task/evidence ref to the underlying source record (masked PII)."""
    con = db()
    try:
        if "|" in ref:
            ar, d = ref.split("|", 1)
            df = pd.read_sql("SELECT stay_date, bar_inr, comp_set_avg_inr, rooms_sold, capacity, remaining, occupancy_pct, pickup_last_7d, "
                             "forecast_rooms, restrictions, group_block_rooms, channel_mix_ota_pct, event_flag, notes, pms_rooms_all, pms_rooms_as_of "
                             "FROM v_hotel_date WHERE ar_hotel_id=? AND stay_date=?", con, params=(ar, d))
            return {"source": "rate_pace.csv (AxisRooms)", **(df.iloc[0].to_dict() if len(df) else {"error": "not found"})}
        if ref.startswith("BK-"):
            df = pd.read_sql("SELECT booking_id, hotel_id, guest_id, guest_name_masked, channel, rate_plan, adr_inr, arrival_date, departure_date, "
                             "status, created_at_ist, created_after_as_of, group_code FROM bookings WHERE booking_id=?", con, params=(ref,))
            return {"source": "bookings.csv (Hotelogix PMS)", **(df.iloc[0].to_dict() if len(df) else {"error": "not found"})}
        df = pd.read_sql("SELECT hotel_id, hotel_name, brand, city, rooms FROM hotels WHERE hotel_id=?", con, params=(ref,))
        return {"source": "property_master.csv", **(df.iloc[0].to_dict() if len(df) else {"error": "not found"})}
    finally:
        con.close()


def approvals() -> dict:
    f = OUT / "approvals.json"
    return json.loads(f.read_text()) if f.exists() else {}


def approve(task_id: str, who: str):
    a = approvals()
    a[task_id] = {"approved_by": who, "approved_at": datetime.now(C.IST).isoformat(timespec="seconds")}
    (OUT / "approvals.json").write_text(json.dumps(a, indent=1))


# --------------------------------------------------------------------------- views
def task_card(t: dict, key: str, ev_lookup: dict):
    ap = approvals().get(t["task_id"])
    status = "In Progress" if ap else t["status"]
    head = f"{PRIO_ICON[t['priority']]} {t['priority']} · {t['title']} — due {fmt_dt(t['due_at'])}"
    with st.expander(head):
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f"**Owner**  \n{t['owner_role']}")
        c2.markdown(f"**Status**  \n{status}")
        c3.markdown(f"**Source**  \n{t['source_agent']}")
        c4.markdown(f"**Task ID**  \n`{t['task_id']}`")
        st.markdown(t["description"])
        st.markdown("**Evidence** — resolved from the source records, not from the model's text")
        for ref in t["evidence_refs"]:
            rec = resolve(ref)
            with st.container(border=True):
                st.markdown(f"`{ref}` · {rec.pop('source')}")
                st.dataframe(pd.DataFrame([rec]), hide_index=True, width="stretch")
        if t["requires_human_approval"]:
            who = st.session_state.get("approver", "").strip()
            if ap:
                st.success(f"Approved by {ap['approved_by']} at {ap['approved_at']}. Approval is recorded here only; nothing is pushed to AxisRooms or the PMS.")
            else:
                st.button("Approve (records approval only — nothing is pushed)", key=f"ap_{key}", disabled=not who,
                          help="Enter your name in the sidebar first." if not who else None,
                          on_click=approve, args=(t["task_id"], who))


def opp_table(opps: list[dict], with_hotel=False):
    rows = [{"": PRIO_ICON[o["priority"]], "Priority": o["priority"], "Date": o["date"], **({"Hotel": o["hotel_id"]} if with_hotel else {}),
             "Agent": AGENT_LABEL[o["agent"]] if "agent" in o else "", "Why": o["why"]} for o in opps]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Why": st.column_config.TextColumn(width="large")})


def overview(b, dq):
    st.subheader("Your 60 seconds")
    tasks = {t["task_id"]: t for h in b["hotels"] for t in h["tasks"]}
    names = {h["hotel_id"]: h["name"] for h in b["hotels"]}
    for tid in b["top_task_ids"][:5]:
        t = tasks[tid]
        st.markdown(f"{PRIO_ICON[t['priority']]} **{names[t['hotel_id']]}** — {t['title']}  \n"
                    f"<small>{t['owner_role']} · due {fmt_dt(t['due_at'])} · {t['priority']}</small>", unsafe_allow_html=True)
    st.divider()
    cols = st.columns(len(b["hotels"]))
    for col, h in zip(cols, b["hotels"]):
        with col:
            hi = sum(o["priority"] == "High" for o in h["ranked_opportunities"])
            conf = [a["confidence"] for a in h["agents"].values()]
            st.markdown(f"### {h['name']}")
            st.caption(f"{h['brand']} · {h['city']} · {h['capacity']} rooms")
            m1, m2, m3 = st.columns(3)
            m1.metric("High opps", hi)
            m2.metric("Tasks", len(h["tasks"]))
            m3.metric("Confidence", f"{min(conf):.2f}")
            for ag, out in h["agents"].items():
                st.markdown(f"**{AGENT_LABEL[ag]}**  \n{out['summary']}")
    st.divider()
    st.subheader("Cluster-level data caveats")
    for f in [f for f in dq if f["severity"] == "high"]:
        st.error(f"**{f['id']}** {f['summary']}")
    st.caption("See the Data quality tab of each hotel for the full report.")


def hotel_view(h, b, audit, dq, dq_md):
    st.header(f"{h['name']}")
    st.caption(f"{h['hotel_id']} · {h['brand']} · {h['city']} · {h['capacity']} rooms")
    ev_lookup = {}
    conf = [a["confidence"] for a in h["agents"].values()]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Tasks open", len(h["tasks"]))
    k2.metric("High-priority opportunities", sum(o["priority"] == "High" for o in h["ranked_opportunities"]))
    k3.metric("Lowest agent confidence", f"{min(conf):.2f}")
    k4.metric("Data warnings (this hotel)", sum(1 for f in dq if f["hotel_id"] == h["hotel_id"] and f["severity"] in ("high", "warn")))
    tabs = st.tabs(["Briefing", "Tasks & evidence", "Rate pace", "Data quality", "Audit & guardrails", "Contract JSON"])

    with tabs[0]:
        st.subheader("Ranked revenue opportunities")
        if h["ranked_opportunities"]:
            opp_table(h["ranked_opportunities"])
        else:
            st.info("Neither agent found a date that warrants action under the guardrails.")
        for ag, out in h["agents"].items():
            st.subheader(AGENT_LABEL[ag])
            c1, c2 = st.columns([4, 1])
            c1.markdown(out["summary"])
            c2.metric("Confidence", f"{out['confidence']:.2f}")
            if out["recommended_actions"]:
                st.dataframe(pd.DataFrame([{**a, "due_at": fmt_dt(a["due_at"])} for a in out["recommended_actions"]]),
                             hide_index=True, width="stretch", column_config={"rationale": st.column_config.TextColumn(width="large")})
            else:
                st.caption("No recommended actions.")
            if out["data_gaps"]:
                with st.expander(f"Data gaps stated by the agent ({len(out['data_gaps'])})"):
                    for g in out["data_gaps"]:
                        st.markdown(f"- {g}")

    with tabs[1]:
        st.caption("Every task carries evidence refs that resolve to real records (booking_id / AxisRooms hotel+date / hotel id).")
        if not h["tasks"]:
            st.info("No tasks for this hotel.")
        for i, t in enumerate(h["tasks"]):
            task_card(t, f"{h['hotel_id']}_{i}", ev_lookup)

    with tabs[2]:
        con = db()
        df = pd.read_sql("SELECT stay_date, bar_inr AS BAR, comp_set_avg_inr AS comp_avg, occupancy_pct AS occ_pct, remaining, pickup_last_7d AS pickup_7d, "
                         "group_block_rooms AS group_rooms, channel_mix_ota_pct AS ota_pct, restrictions, event_flag, notes "
                         "FROM v_hotel_date WHERE hotel_id=? ORDER BY stay_date", con, params=(h["hotel_id"],))
        con.close()
        c1, c2 = st.columns(2)
        c1.markdown("**BAR vs comp-set average (INR)**")
        c1.line_chart(df.set_index("stay_date")[["BAR", "comp_avg"]])
        c2.markdown("**Occupancy % and rooms remaining**")
        c2.bar_chart(df.set_index("stay_date")[["occ_pct"]])
        st.dataframe(df, hide_index=True, width="stretch")

    with tabs[3]:
        mine = [f for f in dq if f["hotel_id"] in (h["hotel_id"], None)]
        st.markdown(f"{sum(f['severity']=='high' for f in mine)} high · {sum(f['severity']=='warn' for f in mine)} warn · {sum(f['severity']=='info' for f in mine)} info "
                    "(hotel-specific plus cluster-wide). Nothing was dropped silently.")
        for f in mine:
            icon = {"high": "🔴", "warn": "🟠", "info": "🔵"}[f["severity"]]
            with st.expander(f"{icon} {f['id']} · {f['category']} — {f['summary'][:110]}"):
                st.markdown(f"**Found:** {f['summary']}")
                st.markdown(f"**What the pipeline did:** {f['action_taken']}")
                st.markdown(f"**Source:** {f['source']}")
                if f["refs"]:
                    st.markdown("**Records:** " + ", ".join(f"`{r}`" for r in f["refs"][:15]))
        st.download_button("Download full data quality report (.md)", dq_md, file_name="dq_report.md")

    with tabs[4]:
        mine = [a for a in audit if a["hotel_id"] == h["hotel_id"]]
        st.caption("What the deterministic post-check changed, and what each model call cost. PII scan: every prompt was checked against all raw guest names, phones and emails before sending.")
        for a in mine:
            st.subheader(AGENT_LABEL[a["agent"]])
            est = " (estimate)" if a["tokens_are_estimates"] else ""
            st.markdown(f"Provider **{a['provider']}** · model `{a['model']}` · prompt `{a['prompt_sha']}` · from cache: {a['cached']} · "
                        f"tokens in/out {a['input_tokens']} / {a['output_tokens']}{est}")
            if a["error"]:
                st.error(a["error"])
            if a["interventions"]:
                st.dataframe(pd.DataFrame(a["interventions"])[["kind", "effect", "rule", "target", "detail"]], hide_index=True, width="stretch")
            else:
                st.caption("No guardrail interventions.")
            if a["rejected_actions"]:
                st.warning(f"Rejected actions: {a['rejected_actions']}")

    with tabs[5]:
        for ag, out in h["agents"].items():
            st.markdown(f"**{AGENT_LABEL[ag]}**")
            st.json(out, expanded=False)
        st.download_button("Download this hotel's agent outputs (JSON)", json.dumps(list(h["agents"].values()), indent=2), file_name=f"{h['hotel_id']}_agents.json")


# --------------------------------------------------------------------------- main
ensure_outputs()
data = load((OUT / "briefing.json").stat().st_mtime)
b, audit, dq, dq_md = data["briefing"], data["audit"], data["dq"], data["dq_md"]

st.title("Revenue Intelligence Briefing")
st.caption(f"Cluster Revenue Manager · as of {fmt_dt(b['as_of'])} (fixed briefing time, not the system clock) · "
           "Recommend, don't act — nothing here changes a rate in the PMS or AxisRooms.")
if "mock-heuristic" in b["providers"]:
    st.warning("The judgment layer ran in **MOCK mode** (a heuristic stand-in, not an LLM) because no API key / cached LLM responses were available. "
               "Set ANTHROPIC_API_KEY and run `python -m revintel.pipeline --provider anthropic` for real model output.")
else:
    st.caption(f"Judgment by: {', '.join(b['models'])} · providers: {b['providers']}")

with st.sidebar:
    st.header("Hotel")
    options = ["Cluster overview"] + [f"{h['name']} · {h['city']}" for h in b["hotels"]]
    choice = st.selectbox("Select", options, label_visibility="collapsed")
    st.text_input("Your name (to approve tasks)", key="approver")
    st.divider()
    st.caption("Approving a task records your name locally. It does not push anything to AxisRooms or the PMS.")

if choice == "Cluster overview":
    overview(b, dq)
else:
    hotel_view(b["hotels"][options.index(choice) - 1], b, audit, dq, dq_md)
