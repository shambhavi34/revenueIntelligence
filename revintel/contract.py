"""The house output contract (REVENUE_ASSIGNMENT.md section 2.2) as a strict pydantic model.

`extra="forbid"`: an agent output with any additional or missing key is invalid. Everything the pipeline needs
beyond the contract (guardrail interventions, per-action evidence refs, token usage) lives in a *separate*
audit record, never in the contract object.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import config as C

AGENTS = ("pricing_optimization", "channel_inventory_restriction")


class Opportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str
    priority: Literal["High", "Medium", "Low"]
    why: str

    @field_validator("date")
    @classmethod
    def _d(cls, v):
        datetime.strptime(v, "%Y-%m-%d")
        return v


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    owner_role: str
    due_at: str
    rationale: str

    @field_validator("due_at")
    @classmethod
    def _due(cls, v):
        datetime.fromisoformat(v)
        return v


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    ref: str
    detail: str

    @field_validator("source")
    @classmethod
    def _s(cls, v):
        if v not in C.SOURCES:
            raise ValueError(f"unknown source {v}")
        return v


class AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: Literal["pricing_optimization", "channel_inventory_restriction"]
    hotel_id: str
    as_of: str
    summary: str
    identified_opportunities: list[Opportunity]
    recommended_actions: list[Action]
    evidence: list[Evidence]
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_approval: bool
    data_gaps: list[str]

    @field_validator("as_of")
    @classmethod
    def _asof(cls, v):
        if v != C.AS_OF_ISO:
            raise ValueError(f"as_of must be {C.AS_OF_ISO}")
        return v


# --- what the LLM is asked to return (a superset: needs a few fields code uses to verify the action) -------
_ev = {"type": "object", "properties": {"source": {"type": "string", "enum": sorted(C.SOURCES)},
                                        "ref": {"type": "string"}, "detail": {"type": "string"}},
       "required": ["source", "ref", "detail"]}
_opp = {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD, must be one of the dates in FACTS"},
                                         "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                                         "why": {"type": "string"}},
        "required": ["date", "priority", "why"]}


def draft_schema(agent: str) -> dict:
    common = {
        "target_date": {"type": ["string", "null"], "description": "YYYY-MM-DD from FACTS, or null for a hotel-level action"},
        "action": {"type": "string", "description": "one-line human-readable action"},
        "urgency": {"type": "string", "enum": ["High", "Medium", "Low"]},
        "rationale": {"type": "string", "description": "why, citing figures that appear in FACTS only"},
        "evidence_refs": {"type": "array", "items": {"type": "object", "properties": {
            "source": {"type": "string", "enum": sorted(C.SOURCES)}, "ref": {"type": "string"}}, "required": ["source", "ref"]},
            "description": "records that support this action; refs must appear in FACTS"},
    }
    if agent == "pricing_optimization":
        action = {"type": "object", "properties": {
            "action_type": {"type": "string", "enum": ["increase_bar", "decrease_bar", "flag_comp_set_control_tower", "review_pricing_position"]},
            "new_bar_inr": {"type": ["integer", "null"], "description": "proposed BAR in INR for increase/decrease, else null"},
            **common}, "required": ["action_type", "target_date", "action", "urgency", "rationale", "evidence_refs"]}
    else:
        action = {"type": "object", "properties": {
            "action_type": {"type": "string", "enum": ["set_minlos", "close_rate_plan", "review_channel_mix"]},
            "min_los": {"type": ["integer", "null"], "description": "nights for set_minlos (2-5), else null"},
            "rate_plan": {"type": ["string", "null"], "description": "plan code to close for close_rate_plan, else null"},
            **common}, "required": ["action_type", "target_date", "action", "urgency", "rationale", "evidence_refs"]}
    return {"type": "object", "properties": {
        "summary": {"type": "string", "description": "3-5 sentences a Revenue Manager can read in 20 seconds"},
        "identified_opportunities": {"type": "array", "items": _opp, "description": "ranked, most important first"},
        "recommended_actions": {"type": "array", "items": action},
        "evidence": {"type": "array", "items": _ev},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
    }, "required": ["summary", "identified_opportunities", "recommended_actions", "evidence", "confidence", "data_gaps"]}
