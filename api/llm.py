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


@dataclass
class LLMResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0
    cached: bool = False

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
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                break
            except retryable as e:
                if attempt == len(RETRY_DELAYS):
                    raise LLMUnavailable(f"{type(e).__name__} after {attempt + 1} tries") from e
                time.sleep(RETRY_DELAYS[attempt])
        msg = resp.choices[0].message
        raw = {"content": msg.content, "tokens": resp.usage.total_tokens if resp.usage else 0,
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
    return LLMResult(raw["content"], calls, 0 if cached else raw["tokens"], cached)


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
        return r
