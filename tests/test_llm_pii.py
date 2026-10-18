import json
from types import SimpleNamespace

import pytest

from conftest import HYD

from revintel import config as C
from revintel import facts as F
from revintel import prompts
from revintel.contract import draft_schema
from revintel.llm import AnthropicLLM, CacheMiss, CachedLLM, OpenAILLM
from revintel.pii import PIILeak, assert_prompt_clean, find_pii

AGENTS = ("pricing_optimization", "channel_inventory_restriction")


def test_no_prompt_contains_guest_pii(ds):
    for agent in AGENTS:
        sysp = prompts.system_prompt(agent)
        for hid in ds.hotel_ids():
            text = sysp + prompts.user_prompt(F.build_facts(ds, ds.findings, hid, agent))
            assert_prompt_clean(text, ds.raw_pii)
            assert not find_pii(text, ds.raw_pii)


def test_scanner_blocks_a_leak(ds):
    row = next(iter(ds.raw_pii["names"])), next(iter(ds.raw_pii["phones"])), next(iter(ds.raw_pii["emails"]))
    for v in row:
        with pytest.raises(PIILeak):
            assert_prompt_clean(f"... guest {v} requested a late checkout ...", ds.raw_pii)
    assert find_pii("Sneha Reddy approved it", {"names": {"Neha Reddy"}, "phones": set(), "emails": set()}) == []   # whole-token only


def test_system_prompt_carries_the_rules_and_grounding_contract():
    s = prompts.system_prompt("pricing_optimization")
    for k in ("§4.2", "§4.4", "§4.3", "submit_briefing", "ONLY the data in FACTS"):
        assert k in s
    assert s == prompts.system_prompt("pricing_optimization")      # static => cacheable


# ----- Anthropic provider with a stub client (no network) ----------------------------------------
class StubClient:
    def __init__(self):
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls += 1
        assert kw["tool_choice"] == {"type": "tool", "name": "submit_briefing"}
        assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
        block = SimpleNamespace(type="tool_use", input={"summary": "s", "identified_opportunities": [], "recommended_actions": [],
                                                       "evidence": [], "confidence": 0.5, "data_gaps": []})
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=2100, output_tokens=310,
                                                                       cache_read_input_tokens=0, cache_creation_input_tokens=800))


def test_anthropic_provider_cache_and_replay(tmp_path):
    stub = StubClient()
    llm = CachedLLM(AnthropicLLM(model=C.MODEL, client=stub), cache_dir=tmp_path)
    schema = draft_schema("pricing_optimization")
    r1 = llm.draft("pricing_optimization", "SYS", "USER", schema)
    assert r1.provider == "anthropic" and r1.usage["input_tokens"] == 2100 and not r1.cached
    r2 = llm.draft("pricing_optimization", "SYS", "USER", schema)
    assert r2.cached and stub.calls == 1                                   # second call served from disk
    replay = CachedLLM(None, cache_dir=tmp_path, replay_only=True)
    assert replay.draft("pricing_optimization", "SYS", "USER", schema).draft["summary"] == "s"
    with pytest.raises(CacheMiss):
        replay.draft("pricing_optimization", "SYS", "DIFFERENT", schema)


def test_api_key_never_in_repo():
    import pathlib, re
    root = pathlib.Path(C.ROOT)
    for p in list(root.rglob("*.py")) + list(root.rglob("*.md")) + list(root.rglob("*.json")) + list(root.rglob("*.example")):
        if ".venv" in p.parts or "venv" in p.parts or "outputs" in p.parts:
            continue
        assert not re.search(r"sk-ant-[A-Za-z0-9_-]{10,}", p.read_text(errors="ignore")), p


def test_draft_schema_is_valid_json_schema():
    for a in AGENTS:
        s = draft_schema(a)
        json.dumps(s)
        assert set(s["required"]) == {"summary", "identified_opportunities", "recommended_actions", "evidence", "confidence", "data_gaps"}


class OpenAIStub:
    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        assert kw["tool_choice"] == {"type": "function", "function": {"name": "submit_briefing"}}
        assert kw["messages"][0]["role"] == "system"
        args = json.dumps({"summary": "s", "identified_opportunities": [], "recommended_actions": [], "evidence": [],
                           "confidence": 0.5, "data_gaps": []})
        msg = SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments=args))])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=SimpleNamespace(prompt_tokens=2800, completion_tokens=350,
                                                     prompt_tokens_details=SimpleNamespace(cached_tokens=0)))


def test_openai_provider_cache_and_replay(tmp_path):
    stub = OpenAIStub()
    llm = CachedLLM(OpenAILLM(model="gpt-test", client=stub), cache_dir=tmp_path)
    schema = draft_schema("pricing_optimization")
    r1 = llm.draft("pricing_optimization", "SYS", "USER", schema)
    assert r1.provider == "openai" and r1.usage["input_tokens"] == 2800 and r1.draft["summary"] == "s" and not r1.cached
    assert llm.draft("pricing_optimization", "SYS", "USER", schema).cached and stub.calls == 1
    assert CachedLLM(None, cache_dir=tmp_path, replay_only=True).model != "gpt-test"   # replay keys on the configured model name
