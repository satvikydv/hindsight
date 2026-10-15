"""The only module that talks to OpenAI. Swap providers or fake them in tests without touching the agents.

- `LLM.chat` does one function-calling turn. Temperature 0 and a fixed seed for benchmarks (PRD §7).
- Retries 429s, timeouts and 5xx with backoff x3, then raises `LLMUnavailable` (the incident fails as llm_unavailable).
- An optional on-disk cache returns identical prompts for free, so re-running a benchmark costs no tokens.
- `FakeEmbedder` is deterministic and token-overlap based; `ScriptedLLM` replays a policy. Both are for tests.
"""

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from api import config

RETRY_DELAYS = (0.5, 2.0, 6.0)
CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "llm"


class LLMUnavailable(RuntimeError):
    """The model could not be reached after retries."""


class EmbeddingUnavailable(RuntimeError):
    """Embeddings could not be computed; recall falls back to structure only."""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    args_error: str | None = None  # set when the model sent arguments that were not valid JSON


# USD per 1M tokens: (input, cached input, output). From OpenAI's pricing pages; update when prices or models change.
PRICES = {"gpt-4.1": (2.00, 0.50, 8.00), "gpt-4.1-mini": (0.40, 0.10, 1.60)}


def cost_usd(model: str, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float | None:
    """Dollar cost of one call, or None if the model is not in the price table (so totals never silently invent a price)."""
    key = model if model in PRICES else next((m for m in sorted(PRICES, key=len, reverse=True)
                                              if model.startswith(m)), None)
    if key is None:
        return None
    p_in, p_cached, p_out = PRICES[key]
    return ((prompt_tokens - cached_tokens) * p_in + cached_tokens * p_cached + completion_tokens * p_out) / 1e6


@dataclass
class LLMResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0                     # total tokens billed for this call (0 when replayed from the disk cache)
    cached: bool = False                # True when replayed from the on-disk prompt cache: no API call, no cost
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0       # prompt tokens served from OpenAI's prompt cache (billed at the cached rate)
    latency_ms: int = 0
    cost_usd: float | None = 0.0

    @property
    def usage(self) -> dict:
        """Telemetry stored on the assistant Message so any call can be traced and priced later."""
        return {"model": self.model, "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "cached_tokens": self.cached_prompt_tokens, "latency_ms": self.latency_ms,
                "cost_usd": self.cost_usd, "disk_cache": self.cached}

    @property
    def assistant_message(self) -> dict:
        """The message to append to the conversation, in OpenAI wire format."""
        msg: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": t.id, "type": "function",
                                  "function": {"name": t.name, "arguments": json.dumps(t.args)}}
                                 for t in self.tool_calls]
        return msg


class LLM(Protocol):
    def chat(self, tier: str, messages: list[dict], tools: list[dict]) -> LLMResult: ...


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# ---- OpenAI ------------------------------------------------------------------------------------------------------
class OpenAILLM:
    def __init__(self, api_key: str | None = None, cache: bool = False, seed: int = 7, client=None):
        from openai import OpenAI

        self._client = client or OpenAI(api_key=api_key or config.LLM_API_KEY, timeout=60, max_retries=0)
        self._cache = cache
        self._seed = seed

    def _model(self, tier: str) -> str:
        return config.MODEL_STRONG if tier == "strong" else config.MODEL_FAST

    def _key(self, model: str, messages: list[dict], tools: list[dict]) -> Path:
        blob = json.dumps({"m": model, "msgs": messages, "tools": tools, "seed": self._seed}, sort_keys=True)
        return CACHE_DIR / (hashlib.sha256(blob.encode()).hexdigest() + ".json")

    def chat(self, tier: str, messages: list[dict], tools: list[dict]) -> LLMResult:
        import openai

        model = self._model(tier)
        path = self._key(model, messages, tools)
        if self._cache and path.exists():
            return _decode(json.loads(path.read_text(encoding="utf-8")), cached=True)

        kwargs = dict(model=model, messages=messages, temperature=0, seed=self._seed)
        if tools:
            kwargs["tools"] = tools
        retryable = (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError,
                     openai.InternalServerError)
        started = time.monotonic()
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                break
            except retryable as e:
                if attempt == len(RETRY_DELAYS):
                    raise LLMUnavailable(f"{type(e).__name__} after {attempt + 1} tries") from e
                time.sleep(RETRY_DELAYS[attempt])
        latency_ms = int((time.monotonic() - started) * 1000)
        msg = resp.choices[0].message
        u = resp.usage
        total = u.total_tokens if u else 0
        prompt = getattr(u, "prompt_tokens", None)
        prompt = total if prompt is None else prompt            # only a total reported: price it all as input
        raw = {"content": msg.content, "tokens": total, "model": model, "prompt_tokens": prompt,
               "completion_tokens": total - prompt,
               "cached_tokens": getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
               "latency_ms": latency_ms,
               "tool_calls": [{"id": t.id, "name": t.function.name, "arguments": t.function.arguments}
                              for t in (msg.tool_calls or [])]}
        if self._cache:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(raw), encoding="utf-8")
        return _decode(raw)


def _decode(raw: dict, cached: bool = False) -> LLMResult:
    calls = []
    for t in raw["tool_calls"]:
        try:
            args, err = json.loads(t["arguments"] or "{}"), None
            if not isinstance(args, dict):
                args, err = {}, "arguments must be a JSON object"
        except json.JSONDecodeError as e:
            args, err = {}, f"invalid JSON arguments: {e}"
        calls.append(ToolCall(t["id"], t["name"], args, err))
    model = raw.get("model", "")
    if cached:  # replayed from disk: nothing was billed, so report no tokens and no cost
        return LLMResult(raw["content"], calls, 0, True, model, 0, 0, 0, 0, 0.0)
    prompt, completion = raw.get("prompt_tokens", raw["tokens"]), raw.get("completion_tokens", 0)
    ctok = raw.get("cached_tokens", 0)
    return LLMResult(raw["content"], calls, raw["tokens"], False, model, prompt, completion, ctok,
                     raw.get("latency_ms", 0), cost_usd(model, prompt, ctok, completion) if model else None)


class OpenAIEmbedder:
    def __init__(self, api_key: str | None = None, model: str | None = None, dim: int | None = None):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key or config.LLM_API_KEY, timeout=30, max_retries=0)
        self._model = model or config.EMBED_MODEL
        self.dim = dim or config.EMBED_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        import openai

        try:
            resp = self._client.embeddings.create(model=self._model, input=texts)
        except openai.OpenAIError as e:
            raise EmbeddingUnavailable(f"{type(e).__name__}: {str(e)[:120]}") from e
        return [d.embedding for d in resp.data]


# ---- fakes for tests ---------------------------------------------------------------------------------------------
class FakeEmbedder:
    """Deterministic bag-of-words hashing into `dim` buckets, L2-normalised: texts sharing words are close."""

    def __init__(self, dim: int = 64):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"[a-z0-9_.-]+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


class DownEmbedder:
    dim = 64

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailable("embedding API down (simulated)")


class ScriptedLLM:
    """Replays a policy: policy(tier, messages, tools) -> LLMResult. Counts calls and fake tokens for assertions."""

    def __init__(self, policy: Callable[[str, list[dict], list[dict]], LLMResult], tokens_per_call: int = 100):
        self.policy = policy
        self.tokens_per_call = tokens_per_call
        self.calls = 0

    def chat(self, tier: str, messages: list[dict], tools: list[dict]) -> LLMResult:
        self.calls += 1
        r = self.policy(tier, messages, tools)
        r.tokens = r.tokens or self.tokens_per_call
        if not r.model:  # report usage as the real thing would: 90% input, priced from the real table
            r.model = config.MODEL_STRONG if tier == "strong" else config.MODEL_FAST
            r.prompt_tokens = int(r.tokens * 0.9)
            r.completion_tokens = r.tokens - r.prompt_tokens
            r.cost_usd = cost_usd(r.model, r.prompt_tokens, 0, r.completion_tokens)
        return r
