"""LLM providers.

OpenAILLM     - real model (OpenAI), forced function call, same schema.
AnthropicLLM  - real model, forced tool use so the reply is schema-shaped JSON, with prompt caching of the static system prompt.
CachedLLM     - on-disk read-through cache keyed by sha256(model + system + user + schema). Makes reruns free and reproducible,
                and lets a reviewer without an API key replay a real run (commit outputs/llm_cache/ after a live run).
MockLLM       - heuristic stand-in (mock_llm.py) for offline demo and tests. NOT an LLM; every output it produces is labelled.

Provider selection (`get_llm`):  auto = Anthropic if ANTHROPIC_API_KEY, else OpenAI if OPENAI_API_KEY, else cache replay, else mock.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import config as C


class CacheMiss(RuntimeError):
    pass


@dataclass
class LLMResult:
    draft: dict
    provider: str
    model: str
    prompt_sha: str
    usage: dict = field(default_factory=dict)
    cached: bool = False


def prompt_sha(model: str, system: str, user: str, schema: dict) -> str:
    h = hashlib.sha256()
    for part in (model, system, user, json.dumps(schema, sort_keys=True)):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:20]


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)  # rough: ~4 chars/token for English+JSON; real usage replaces it when available


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str | None = None, client=None):
        self.model = model or C.MODEL
        if client is None:
            import anthropic  # imported lazily so mock/replay need no key

            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        self.client = client

    def draft(self, agent: str, system: str, user: str, schema: dict) -> LLMResult:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[{"name": "submit_briefing", "description": "Submit the briefing for this hotel.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": "submit_briefing"},
            messages=[{"role": "user", "content": user}],
        )
        block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if block is None:
            raise RuntimeError("model did not call submit_briefing")
        u = getattr(resp, "usage", None)
        usage = {
            "input_tokens": getattr(u, "input_tokens", None),
            "output_tokens": getattr(u, "output_tokens", None),
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0),
        }
        return LLMResult(dict(block.input), self.name, self.model, prompt_sha(self.model, system, user, schema), usage)


class OpenAILLM:
    """OpenAI Chat Completions with a forced function call (same `submit_briefing` schema as the Anthropic path)."""
    name = "openai"

    def __init__(self, model: str | None = None, client=None):
        self.model = model or C.OPENAI_MODEL
        if client is None:
            import openai  # lazy: only needed when this provider is used

            client = openai.OpenAI()  # reads OPENAI_API_KEY from the environment
        self.client = client

    def draft(self, agent: str, system: str, user: str, schema: dict) -> LLMResult:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[{"type": "function", "function": {"name": "submit_briefing", "description": "Submit the briefing for this hotel.", "parameters": schema}}],
            tool_choice={"type": "function", "function": {"name": "submit_briefing"}},
        )
        calls = getattr(resp.choices[0].message, "tool_calls", None) or []
        if not calls:
            raise RuntimeError("model did not call submit_briefing")
        draft = json.loads(calls[0].function.arguments)
        u = getattr(resp, "usage", None)
        det = getattr(u, "prompt_tokens_details", None)
        usage = {"input_tokens": getattr(u, "prompt_tokens", None), "output_tokens": getattr(u, "completion_tokens", None),
                 "cache_read_input_tokens": getattr(det, "cached_tokens", 0) or 0, "cache_creation_input_tokens": 0}
        return LLMResult(draft, self.name, self.model, prompt_sha(self.model, system, user, schema), usage)


class CachedLLM:
    def __init__(self, inner, cache_dir: Path | None = None, refresh: bool = False, replay_only: bool = False):
        self.inner, self.refresh, self.replay_only = inner, refresh, replay_only
        self.dir = Path(cache_dir or C.CACHE_DIR)
        self.name = getattr(inner, "name", "replay") if inner else "replay"
        self.model = getattr(inner, "model", C.MODEL)

    def draft(self, agent, system, user, schema) -> LLMResult:
        sha = prompt_sha(self.model, system, user, schema)
        f = self.dir / f"{sha}.json"
        if f.exists() and not self.refresh:
            d = json.loads(f.read_text())
            return LLMResult(d["draft"], d["provider"], d["model"], sha, d.get("usage", {}), cached=True)
        if self.replay_only or self.inner is None:
            raise CacheMiss(f"no cached response for prompt {sha} ({agent})")
        res = self.inner.draft(agent, system, user, schema)
        if res.provider in ("anthropic", "openai"):  # only cache real model output
            self.dir.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"prompt_sha": sha, "agent": agent, "provider": res.provider, "model": res.model,
                                     "usage": res.usage, "draft": res.draft}, indent=1))
        return res


class FallbackLLM:
    """Replay from cache if present, otherwise use the mock (labelled)."""

    def __init__(self, cached: CachedLLM, mock):
        self.cached, self.mock, self.name, self.model = cached, mock, "auto-replay-or-mock", C.MODEL

    def draft(self, agent, system, user, schema):
        try:
            return self.cached.draft(agent, system, user, schema)
        except CacheMiss:
            return self.mock.draft(agent, system, user, schema)


def get_llm(provider: str = "auto", refresh: bool = False):
    from .mock_llm import MockLLM

    provider = (provider or "auto").lower()
    if provider == "mock":
        return MockLLM()
    if provider == "replay":
        return CachedLLM(None, replay_only=True)
    if provider == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set. Export it, or use --provider mock / replay.")
        return CachedLLM(AnthropicLLM(), refresh=refresh)
    if provider == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY is not set. Export it, or use --provider mock / replay.")
        return CachedLLM(OpenAILLM(), refresh=refresh)
    if provider == "auto":
        if os.getenv("ANTHROPIC_API_KEY"):
            return CachedLLM(AnthropicLLM(), refresh=refresh)
        if os.getenv("OPENAI_API_KEY"):
            return CachedLLM(OpenAILLM(), refresh=refresh)
        return FallbackLLM(CachedLLM(None, replay_only=True), MockLLM())
    raise SystemExit(f"unknown provider {provider}")
