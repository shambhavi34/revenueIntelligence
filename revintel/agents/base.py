"""Shared agent runner. An agent = (mission prompt, draft schema) + the same deterministic pipeline around the model:

   facts (code) -> prompt (PII-scanned) -> LLM judgment -> post-check (code) -> contract object + tasks
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .. import config as C
from .. import prompts, tasks as T
from ..contract import AgentOutput, draft_schema
from ..facts import build_facts
from ..guardrails import Processed, process_draft
from ..llm import approx_tokens
from ..pii import assert_prompt_clean


@dataclass
class AgentRun:
    output: dict
    tasks: list[dict]
    audit: dict
    facts: dict = field(repr=False, default_factory=dict)
    draft: dict = field(repr=False, default_factory=dict)


class Agent:
    name: str = ""

    def run(self, ds, findings, hotel_id: str, llm) -> AgentRun:
        facts = build_facts(ds, findings, hotel_id, self.name)
        system, user = prompts.system_prompt(self.name), prompts.user_prompt(facts)
        assert_prompt_clean(system + "\n" + user, ds.raw_pii)  # raises PIILeak -> nothing is sent
        schema = draft_schema(self.name)
        audit = {"agent": self.name, "hotel_id": hotel_id, "provider": None, "model": None, "prompt_sha": None, "cached": False,
                 "input_tokens": approx_tokens(system + user), "output_tokens": None, "tokens_are_estimates": True,
                 "interventions": [], "rejected_actions": [], "error": None, "seconds": None}
        t0 = time.time()
        draft, processed = {}, None
        try:
            res = llm.draft(self.name, system, user, schema)
            draft = res.draft
            audit.update(provider=res.provider, model=res.model, prompt_sha=res.prompt_sha, cached=res.cached)
            if res.usage.get("input_tokens"):
                audit.update(input_tokens=res.usage["input_tokens"] + (res.usage.get("cache_read_input_tokens") or 0)
                             + (res.usage.get("cache_creation_input_tokens") or 0),
                             output_tokens=res.usage.get("output_tokens"), tokens_are_estimates=False,
                             usage=res.usage)
            else:
                audit["output_tokens"] = approx_tokens(json.dumps(draft))
            processed = process_draft(self.name, facts, draft)
        except Exception as exc:  # the briefing must still render; the failure is explicit, never silent
            audit["error"] = f"{type(exc).__name__}: {exc}"
        audit["seconds"] = round(time.time() - t0, 2)

        if processed is None:
            out = AgentOutput(agent=self.name, hotel_id=hotel_id, as_of=C.AS_OF_ISO,
                              summary="The agent could not produce a valid briefing for this hotel. No recommendation is made.",
                              identified_opportunities=[], recommended_actions=[], evidence=[], confidence=0.0,
                              requires_human_approval=False,
                              data_gaps=[f"Agent failure: {audit['error']}"]).model_dump()
            return AgentRun(out, [], audit, facts, draft)

        tasks, rejected = T.build_tasks(hotel_id, self.name, processed.extras)
        audit["interventions"] = processed.interventions
        audit["rejected_actions"] = rejected
        return AgentRun(processed.output, tasks, audit, facts, draft)
