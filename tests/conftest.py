import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from revintel import config as C  # noqa: E402
from revintel import facts as F  # noqa: E402
from revintel import ingest  # noqa: E402

HYD, NOV, BLR, PNQ = "ACM-HYD-GCB-03", "NOV-BLR-WFD-02", "ACM-BLR-IND-01", "PRT-PNQ-AMB-04"


@pytest.fixture(scope="session")
def ds():
    d = ingest.build(write_db=False)
    d.findings += F.signal_findings(d)
    d.findings = ingest.finalise_findings(d.findings)
    return d


@pytest.fixture()
def facts(ds):
    def _f(hotel, agent="pricing_optimization"):
        return copy.deepcopy(F.build_facts(ds, ds.findings, hotel, agent))
    return _f


@pytest.fixture()
def tmp_out(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    monkeypatch.setattr(C, "OUT_DIR", out)
    monkeypatch.setattr(C, "DB_PATH", out / "revintel.db")
    monkeypatch.setattr(C, "CACHE_DIR", out / "llm_cache")
    return out


def draft(actions=(), opps=(), evidence=(), summary="ok", confidence=0.8, gaps=()):
    return {"summary": summary, "identified_opportunities": list(opps), "recommended_actions": list(actions),
            "evidence": list(evidence), "confidence": confidence, "data_gaps": list(gaps)}


def price_action(date, new, typ="increase_bar", refs=None, urgency="High", **kw):
    return {"action_type": typ, "new_bar_inr": new, "target_date": date, "action": f"Move BAR to {new}", "urgency": urgency,
            "rationale": "test rationale", "evidence_refs": refs if refs is not None else [], **kw}


def restr_action(typ, date, urgency="High", **kw):
    return {"action_type": typ, "min_los": kw.pop("min_los", None), "rate_plan": kw.pop("rate_plan", None), "target_date": date,
            "action": "x", "urgency": urgency, "rationale": "test rationale", "evidence_refs": [], **kw}
