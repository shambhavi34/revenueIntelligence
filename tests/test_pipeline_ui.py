import json
from datetime import datetime

import pytest

from evals.cases import CASES
from revintel import config as C
from revintel import pipeline, tasks as T


@pytest.fixture()
def res(tmp_out):
    return pipeline.run("mock")


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_eval_case(res, case):
    case.fn(res)


def test_outputs_written(res, tmp_out):
    for f in ("briefing.json", "agent_outputs.json", "tasks.json", "audit.json", "dq_report.json", "dq_report.md", "revintel.db"):
        assert (tmp_out / f).exists(), f
    assert len(json.loads((tmp_out / "agent_outputs.json").read_text())) == 8
    assert len(list((tmp_out / "facts").glob("*.json"))) == 8


def test_agent_failure_is_explicit_not_silent(tmp_out):
    class Boom:
        name, model = "boom", "boom"
        def draft(self, *a, **k):
            raise RuntimeError("api down")
    from revintel.agents import AGENTS
    from revintel import facts as F, ingest
    ds = ingest.build(write_db=False); ds.findings += F.signal_findings(ds); ds.findings = ingest.finalise_findings(ds.findings)
    r = AGENTS[0].run(ds, ds.findings, "ACM-HYD-GCB-03", Boom())
    assert r.output["confidence"] == 0.0 and r.output["recommended_actions"] == [] and "api down" in r.output["data_gaps"][0]
    assert r.audit["error"] and r.tasks == []


def test_due_at_is_sla_derived_and_bounded():
    f = lambda s: datetime.fromisoformat(s)
    assert f(T.due_at("P2", "2026-08-30")) - C.AS_OF == __import__("datetime").timedelta(hours=12)   # section-2 P2 resolution SLA
    assert f(T.due_at("P4", "2026-08-30")) - C.AS_OF == __import__("datetime").timedelta(hours=48)
    assert f(T.due_at("P2", "2026-08-25")) - C.AS_OF == __import__("datetime").timedelta(hours=C.MIN_LEAD_HOURS)   # same-day floor
    assert f(T.due_at("P4", "2026-08-27")).isoformat().startswith("2026-08-26T18:00")                # cap: 18:00 the day before


def test_ui_renders_every_view(res, tmp_out):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(C.ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception
    assert any("MOCK" in w.value for w in at.warning)               # the mock banner must be visible
    for opt in at.sidebar.selectbox[0].options[1:]:
        at.sidebar.selectbox[0].select(opt).run()
        assert not at.exception, opt
        assert len(at.tabs) == 6
