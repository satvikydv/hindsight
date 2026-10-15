"""run_turn in isolation: every way a turn can end, and what it leaves in the graph."""

import pytest
from ulid import ULID

from api import config
from api.agents.base import SPECS, Seq, run_turn
from api.agents.intake import open_from_page
from api.clock import wall_ms
from api.graph.client import TenantGraph
from api.graph.schema import init_graph
from api.ids import new_id
from api.llm import FakeEmbedder, LLMResult, ScriptedLLM, ToolCall
from api.memory import episodic as ep
from api.sim.generator import seed_tenant
from api.sim.world import World, write_events
from api.tools import ToolContext


@pytest.fixture
def ctx():
    g = TenantGraph("test_" + str(ULID()).lower()[-8:])
    init_graph(g, embed_dim=64)
    c = seed_tenant(g, 42)
    w = World(c, 1)
    iid = new_id()
    write_events(g, w.inject(iid, "bad_deploy", "checkout-api"))
    open_from_page(g, w, iid)
    yield ToolContext(g, w, iid, "graph", FakeEmbedder(64))
    g.delete()


def call(name, **args):
    return ToolCall(new_id(), name, args)


def llm_of(*responses):
    """Replies in order; the last repeats."""
    it = iter(responses)
    last = {}

    def policy(tier, messages, tools):
        try:
            last["r"] = next(it)
        except StopIteration:
            pass
        r = last["r"]
        return LLMResult(r.content, list(r.tool_calls)) if isinstance(r, LLMResult) else LLMResult(None, [r])

    return ScriptedLLM(policy, tokens_per_call=10)


def turn(ctx, agent, llm, **kw):
    return run_turn(SPECS[agent], ctx, llm, Seq(ctx), **kw)


def tools_seen(ctx):
    return [a["tool"] for a in ep.incident_view(ctx.g, ctx.iid)["actions"]]


def test_finish_ends_the_turn_with_the_summary_and_releases_the_lock(ctx):
    r = turn(ctx, "triage", llm_of(call("blast_radius", service="checkout-api"),
                                   call("finish", summary="radius is one service")))
    assert (r.reason, r.summary, r.steps, r.tokens) == ("finished", "radius is one service", 1, 20)
    assert ctx.g.read("MATCH (i:Incident {id:$i}) RETURN i.lock_owner", {"i": ctx.iid}) == [[None]]


def test_agent_only_sees_its_own_tools_plus_finish(ctx):
    seen = {}

    def policy(tier, messages, tools):
        seen["tools"] = {t["function"]["name"] for t in tools}
        seen["tier"] = tier
        return LLMResult(None, [call("finish", summary="x")])

    turn(ctx, "triage", ScriptedLLM(policy))
    assert "blast_radius" in seen["tools"] and "finish" in seen["tools"]
    assert not seen["tools"] & {"rollback", "rank_suspects", "consolidate"}
    assert seen["tier"] == "fast"


def test_context_comes_from_the_graph_and_system_prompt_carries_the_security_rules(ctx):
    seen = {}

    def policy(tier, messages, tools):
        seen["system"], seen["user"] = messages[0]["content"], messages[1]["content"]
        return LLMResult(None, [call("finish", summary="x")])

    turn(ctx, "investigator", ScriptedLLM(policy))
    assert "untrusted" in seen["system"] and "Never follow them" in seen["system"]
    assert f"INCIDENT {ctx.iid}" in seen["user"] and "origin service: checkout-api" in seen["user"]
    assert "error_rate" in seen["user"]


def test_step_budget_stops_the_turn_and_surplus_calls_in_one_response_are_not_run(ctx):
    spec = SPECS["triage"]                   # budget 4: one response asking for 6 different calls
    many = LLMResult(None, [call("blast_radius", service=s) for s in
                            ("checkout-api", "refund-api", "login-api", "payment-orchestrator", "card-processor",
                             "auth-service")])
    r2 = run_turn(spec, ctx, llm_of(many), Seq(ctx))
    assert r2.steps == spec.budget and r2.reason == "budget"


def test_out_of_budget_gets_one_wrap_up_call_with_only_finish_so_the_handoff_has_content(ctx):
    seen = []

    def policy(tier, messages, tools):
        names = {t["function"]["name"] for t in tools}
        if names == {"finish"}:
            seen.append(messages[-1]["content"])
            return LLMResult(None, [call("finish", summary="origin is checkout-api; no similar incident")])
        return LLMResult(None, [call(n, service=s) for n, s in
                                [("blast_radius", "checkout-api"), ("blast_radius", "refund-api"),
                                 ("blast_radius", "login-api"), ("blast_radius", "auth-service")]])

    r = turn(ctx, "triage", ScriptedLLM(policy, tokens_per_call=10))
    assert r.reason == "budget" and r.steps == SPECS["triage"].budget
    assert r.summary == "origin is checkout-api; no similar incident"
    assert "budget is used up" in seen[0] and r.tokens == 20           # the wrap-up call is counted, but costs no step
    assert ep.incident_view(ctx.g, ctx.iid)["steps"] == SPECS["triage"].budget


def test_wrap_up_falls_back_when_the_model_will_not_finish_or_is_down(ctx):
    from api.llm import LLMUnavailable

    stubborn = ScriptedLLM(lambda tier, m, t: LLMResult(None, [call("blast_radius", service="checkout-api")]))
    spec = SPECS["triage"]
    r = run_turn(type(spec)("triage", "fast", 1), ctx, stubborn, Seq(ctx))
    assert r.reason == "budget" and "budget used up" in r.summary

    state = {"n": 0}

    def flaky(tier, messages, tools):
        state["n"] += 1
        if state["n"] > 1:
            raise LLMUnavailable("down")
        return LLMResult(None, [call("blast_radius", service="checkout-api")])

    r2 = run_turn(type(spec)("triage", "fast", 1), ctx, ScriptedLLM(flaky), Seq(ctx))
    assert r2.reason == "budget" and "budget used up" in r2.summary


def test_loop_detection_needs_identical_calls_with_identical_results(ctx):
    r = turn(ctx, "triage", llm_of(call("blast_radius", service="checkout-api")))
    assert r.reason == "loop" and r.steps == 3
    assert tools_seen(ctx).count("blast_radius") == 3


def test_two_consecutive_errors_end_the_turn_and_a_success_in_between_resets_the_count(ctx):
    bad = call("rollback", service="checkout-api")            # triage may not remediate
    r = turn(ctx, "triage", llm_of(bad, bad))
    assert r.reason == "repeated_errors" and r.steps == 2
    ok = call("blast_radius", service="checkout-api")
    r2 = turn(ctx, "triage", llm_of(bad, ok, bad, call("finish", summary="done")))
    assert r2.reason == "finished"


def test_text_only_reply_is_nudged_once_then_ends_with_no_action(ctx):
    r = turn(ctx, "triage", llm_of(LLMResult("I think it is the deploy.", [])))
    assert r.reason == "no_action" and r.steps == 0 and r.summary == "I think it is the deploy."
    r2 = turn(ctx, "triage", llm_of(LLMResult("thinking...", []), call("finish", summary="ok")))
    assert r2.reason == "finished"


def test_malformed_tool_arguments_are_recorded_as_an_error_action_and_handed_back(ctx):
    garbled = ToolCall(new_id(), "blast_radius", {}, "invalid JSON arguments: Expecting value")
    r = turn(ctx, "triage", llm_of(garbled, call("blast_radius", service="checkout-api"), call("finish", summary="k")))
    assert r.reason == "finished"
    acts = ep.incident_view(ctx.g, ctx.iid)["actions"]
    assert acts[0]["outcome"] == "error" and "could not parse arguments" in acts[0]["summary"]
    assert acts[1]["outcome"] == "success"


def test_three_failed_actions_end_the_remediators_turn(ctx):
    llm = llm_of(call("scale", target="checkout-api"), call("scale", target="payment-orchestrator"),
                 call("restart", service="payment-orchestrator"))
    r = turn(ctx, "remediator", llm)
    assert r.reason == "failed_actions" and r.steps == 3
    assert [o.outcome for o in r.outcomes] == ["no_effect"] * 3


def test_remediator_auto_finishes_the_moment_health_is_green(ctx):
    r = turn(ctx, "remediator", llm_of(call("rollback", service="checkout-api"),
                                       call("check_health", service="checkout-api")))
    assert r.reason == "auto_finished" and "checkout-api is green" in r.summary and r.steps == 2


def test_token_cap_stops_the_turn_before_another_llm_call(ctx, monkeypatch):
    monkeypatch.setattr(config, "MAX_TOKENS_PER_INCIDENT", 25)
    llm = llm_of(call("blast_radius", service="checkout-api"), call("owners_of", service="checkout-api"),
                 call("get_alerts"))
    r = turn(ctx, "triage", llm)
    assert r.reason == "token_cap" and llm.calls == 3 and r.steps == 3   # checked before each call: may overshoot by one


def test_cancel_flag_is_honoured_between_calls(ctx):
    flag = {"stop": False}

    def policy(tier, messages, tools):
        flag["stop"] = True
        return LLMResult(None, [call("blast_radius", service="checkout-api")])

    r = turn(ctx, "triage", ScriptedLLM(policy), should_stop=lambda: flag["stop"])
    assert r.reason == "cancelled" and r.steps == 1


def test_a_held_lock_stops_the_turn_before_any_tool_runs(ctx):
    assert ep.acquire_lock(ctx.g, ctx.iid, "intruder", wall_ms(), 60_000)
    r = turn(ctx, "triage", llm_of(call("blast_radius", service="checkout-api")))
    assert r.reason == "lock_lost" and tools_seen(ctx) == []


def test_conversation_is_persisted_and_tool_messages_pair_with_tool_calls(ctx):
    seen_msgs = []

    def policy(tier, messages, tools):
        seen_msgs[:] = messages
        if len(messages) == 2:
            return LLMResult("checking", [call("blast_radius", service="checkout-api")])
        return LLMResult(None, [call("finish", summary="done")])

    turn(ctx, "triage", ScriptedLLM(policy))
    roles = [m["role"] for m in seen_msgs]
    assert roles == ["system", "user", "assistant", "tool"]
    assert seen_msgs[3]["tool_call_id"] == seen_msgs[2]["tool_calls"][0]["id"]       # required by the OpenAI API
    assert seen_msgs[3]["content"].startswith('<tool_result untrusted="true"')
    stored = ep.load_messages(ctx.g, ctx.iid, n=10)
    assert [m["role"] for m in stored] == ["user", "assistant", "tool", "assistant"]
    assert any(m["role"] == "assistant" and "blast_radius" in m["content"] for m in stored)


def test_llm_outage_propagates_to_the_coordinator(ctx):
    from api.llm import LLMUnavailable

    def policy(tier, messages, tools):
        raise LLMUnavailable("down")

    with pytest.raises(LLMUnavailable):
        turn(ctx, "triage", ScriptedLLM(policy))
