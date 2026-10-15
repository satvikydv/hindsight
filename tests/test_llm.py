import math
from types import SimpleNamespace

import httpx
import openai
import pytest

from api import config, llm
from api.llm import (DownEmbedder, EmbeddingUnavailable, FakeEmbedder, LLMResult, LLMUnavailable, OpenAILLM,
                     ScriptedLLM, ToolCall, _decode)


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))  # vectors are already normalised


def test_fake_embedder_is_deterministic_normalised_and_overlap_sensitive():
    e = FakeEmbedder(dim=64)
    a, a2, near, far = e.embed(["checkout-api error_rate spike", "checkout-api error_rate spike",
                                "checkout-api error_rate latency", "kafka disk full"])
    assert a == a2
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0)
    assert cosine(a, near) > cosine(a, far)


def test_down_embedder_raises_embedding_unavailable():
    with pytest.raises(EmbeddingUnavailable):
        DownEmbedder().embed(["x"])


def test_assistant_message_round_trips_tool_calls_in_wire_format():
    r = LLMResult("thinking", [ToolCall("c1", "blast_radius", {"service": "checkout-api"})])
    assert r.assistant_message == {"role": "assistant", "content": "thinking", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "blast_radius", "arguments": '{"service": "checkout-api"}'}}]}
    assert LLMResult("hi").assistant_message == {"role": "assistant", "content": "hi"}


def test_decode_flags_bad_arguments_instead_of_crashing():
    raw = {"content": None, "tokens": 12, "tool_calls": [
        {"id": "1", "name": "a", "arguments": '{"x": 1}'},
        {"id": "2", "name": "b", "arguments": "{not json"},
        {"id": "3", "name": "c", "arguments": "[1, 2]"},
        {"id": "4", "name": "d", "arguments": ""}]}
    r = _decode(raw)
    assert r.tokens == 12 and [t.name for t in r.tool_calls] == ["a", "b", "c", "d"]
    assert r.tool_calls[0].args == {"x": 1} and r.tool_calls[0].args_error is None
    assert r.tool_calls[1].args == {} and "invalid JSON" in r.tool_calls[1].args_error
    assert r.tool_calls[2].args_error == "arguments must be a JSON object"
    assert r.tool_calls[3].args == {} and r.tool_calls[3].args_error is None


def test_scripted_llm_counts_calls_and_fills_tokens():
    s = ScriptedLLM(lambda tier, m, t: LLMResult("ok"), tokens_per_call=50)
    assert s.chat("fast", [], []).tokens == 50 and s.calls == 1


class StubClient:
    """Mimics client.chat.completions.create; each item in `script` is a response or an exception to raise."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def reply(content="done", tool_calls=None, tokens=42):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(total_tokens=tokens))


def conn_error():
    return openai.APIConnectionError(request=httpx.Request("POST", "http://x"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_DELAYS", (0, 0, 0))


def test_retries_transient_errors_then_succeeds():
    stub = StubClient([conn_error(), conn_error(), reply("recovered")])
    r = OpenAILLM(client=stub).chat("fast", [{"role": "user", "content": "hi"}], [])
    assert r.content == "recovered" and r.tokens == 42 and stub.calls == 3


def test_gives_up_after_three_retries_with_llm_unavailable():
    stub = StubClient([conn_error()] * 4)
    with pytest.raises(LLMUnavailable):
        OpenAILLM(client=stub).chat("fast", [], [])
    assert stub.calls == 4


def test_non_transient_errors_are_not_retried():
    bad = openai.BadRequestError("bad", response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                                 body=None)
    stub = StubClient([bad, reply()])
    with pytest.raises(openai.BadRequestError):
        OpenAILLM(client=stub).chat("fast", [], [])
    assert stub.calls == 1


def test_tool_calls_are_parsed_from_the_response():
    tc = SimpleNamespace(id="call_1", function=SimpleNamespace(name="get_alerts", arguments="{}"))
    r = OpenAILLM(client=StubClient([reply(None, [tc])])).chat("strong", [], [])
    assert r.content is None and [(t.id, t.name, t.args) for t in r.tool_calls] == [("call_1", "get_alerts", {})]


def test_cache_returns_identical_prompts_without_calling_the_api(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path)
    stub = StubClient([reply("first", tokens=77)])
    model = OpenAILLM(client=stub, cache=True)
    msgs = [{"role": "user", "content": "same prompt"}]
    first = model.chat("fast", msgs, [])
    again = model.chat("fast", msgs, [])
    assert (first.content, first.tokens, first.cached) == ("first", 77, False)
    assert (again.content, again.tokens, again.cached) == ("first", 0, True)  # cached replies cost no tokens
    assert stub.calls == 1
    other = StubClient([reply("different")])
    assert OpenAILLM(client=other, cache=True).chat("fast", [{"role": "user", "content": "new"}], []).content == "different"
    assert OpenAILLM(client=StubClient([reply("nocache")]), cache=False).chat("fast", msgs, []).content == "nocache"


def detailed(content="ok", prompt=1000, cached=400, completion=100):
    usage = SimpleNamespace(total_tokens=prompt + completion, prompt_tokens=prompt, completion_tokens=completion,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=cached))
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))],
                           usage=usage)


def test_usage_split_latency_and_cost_are_captured_per_call(monkeypatch):
    monkeypatch.setattr(config, "MODEL_STRONG", "gpt-4.1")
    monkeypatch.setattr(config, "MODEL_FAST", "gpt-4.1-mini")
    strong = OpenAILLM(client=StubClient([detailed()])).chat("strong", [], [])
    # 600 uncached input x $2.00 + 400 cached input x $0.50 + 100 output x $8.00, per 1M tokens
    assert (strong.model, strong.prompt_tokens, strong.cached_prompt_tokens, strong.completion_tokens) == (
        "gpt-4.1", 1000, 400, 100)
    assert strong.cost_usd == pytest.approx((600 * 2.00 + 400 * 0.50 + 100 * 8.00) / 1e6)
    fast = OpenAILLM(client=StubClient([detailed()])).chat("fast", [], [])
    assert fast.cost_usd == pytest.approx((600 * 0.40 + 400 * 0.10 + 100 * 1.60) / 1e6)
    assert strong.latency_ms >= 0 and strong.usage["cost_usd"] == strong.cost_usd and strong.usage["model"] == "gpt-4.1"


def test_cost_usd_handles_dated_model_names_and_never_invents_a_price():
    assert llm.cost_usd("gpt-4.1-2025-04-14", 1_000_000, 0, 0) == pytest.approx(2.00)
    assert llm.cost_usd("gpt-4.1-mini-2025-04-14", 1_000_000, 0, 0) == pytest.approx(0.40)   # not mistaken for gpt-4.1
    assert llm.cost_usd("some-future-model", 1000, 0, 100) is None


def test_a_response_that_reports_only_a_total_is_priced_as_all_input(monkeypatch):
    monkeypatch.setattr(config, "MODEL_FAST", "gpt-4.1-mini")
    r = OpenAILLM(client=StubClient([reply(tokens=1000)])).chat("fast", [], [])
    assert (r.prompt_tokens, r.completion_tokens) == (1000, 0) and r.cost_usd == pytest.approx(1000 * 0.40 / 1e6)


def test_unknown_model_is_flagged_unpriced_not_zero(monkeypatch):
    monkeypatch.setattr(config, "MODEL_FAST", "mystery-model")
    assert OpenAILLM(client=StubClient([detailed()])).chat("fast", [], []).cost_usd is None


def test_a_disk_cache_replay_reports_no_tokens_and_no_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "MODEL_FAST", "gpt-4.1-mini")
    model = OpenAILLM(client=StubClient([detailed()]), cache=True)
    first = model.chat("fast", [{"role": "user", "content": "x"}], [])
    again = model.chat("fast", [{"role": "user", "content": "x"}], [])
    assert first.cost_usd > 0 and first.tokens == 1100
    assert (again.cached, again.tokens, again.cost_usd, again.prompt_tokens) == (True, 0, 0.0, 0)
    assert again.usage["disk_cache"] is True


def test_scripted_llm_reports_priced_usage_like_the_real_thing():
    r = ScriptedLLM(lambda t, m, tools: LLMResult("hi"), tokens_per_call=100).chat("strong", [], [])
    assert (r.prompt_tokens, r.completion_tokens) == (90, 10)
    assert r.cost_usd == pytest.approx((90 * 2.00 + 10 * 8.00) / 1e6)


@pytest.mark.live
@pytest.mark.skipif(not config.LLM_API_KEY, reason="no OPENAI key in .env")
def test_live_function_calling_and_embeddings():
    tools = [{"type": "function", "function": {
        "name": "blast_radius", "description": "Services hurt if a service fails",
        "parameters": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]}}}]
    r = OpenAILLM().chat("fast", [{"role": "user", "content": "What is the blast radius of checkout-api?"}], tools)
    assert [t.name for t in r.tool_calls] == ["blast_radius"] and r.tool_calls[0].args == {"service": "checkout-api"}
    assert r.tokens > 0
    vec = llm.OpenAIEmbedder().embed(["checkout-api error rate"])[0]
    assert len(vec) == config.EMBED_DIM
