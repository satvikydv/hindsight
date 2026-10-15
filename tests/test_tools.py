import json

import pytest
from ulid import ULID

from api.agents.intake import open_from_page
from api.graph.client import TenantGraph
from api.graph.schema import init_graph
from api.ids import new_id
from api.llm import FakeEmbedder
from api.memory import episodic as ep
from api.memory import procedural as pr
from api.sim.generator import seed_tenant
from api.sim.scenarios import build_scenarios
from api.sim.world import INJECTION_LINE, World, write_events
from api.tools import REGISTRY, ToolContext, execute, schemas_for
from api.tools.base import MAX_RESULT_CHARS, validate


@pytest.fixture
def env():
    g = TenantGraph("test_" + str(ULID()).lower()[-8:])
    init_graph(g, embed_dim=64)
    c = seed_tenant(g, 42)
    yield g, c, World(c, 1)
    g.delete()


def start(env, fault_class, service=None, mode="graph", **kw):
    g, c, w = env
    iid = new_id()
    write_events(g, w.inject(iid, fault_class, service, **kw))
    page = open_from_page(g, w, iid)
    ctx = ToolContext(g, w, iid, mode=mode, embedder=FakeEmbedder(64))
    ctx.page = page
    return ctx


_seq = iter(range(1, 10_000))


def run(ctx, agent, name, **args):
    return execute(ctx, agent, name, args, seq=next(_seq))


def actions(ctx):
    return ep.incident_view(ctx.g, ctx.iid)["actions"]


# ---- registry / validation ----------------------------------------------------------------------------------------
def test_every_schema_is_openai_valid_and_each_agent_has_tools():
    for t in REGISTRY.values():
        s = t.schema()["function"]["parameters"]
        assert s["type"] == "object" and set(s["required"]) <= set(s["properties"])
        for p in s["properties"].values():
            assert p["type"] in {"string", "integer", "number", "boolean", "array", "object"}
            if p["type"] == "array":
                assert "items" in p  # OpenAI rejects arrays without items
    for agent in ("triage", "investigator", "remediator"):
        assert schemas_for(agent)
    assert {s["function"]["name"] for s in schemas_for("remediator")} >= {"rollback", "check_health", "playbook_steps"}
    assert "rollback" not in {s["function"]["name"] for s in schemas_for("triage")}
    # the Remediator can verify a playbook's precondition before it acts, but cannot rank or confirm causes
    names = {s["function"]["name"] for s in schemas_for("remediator")}
    assert {"recent_changes", "get_metrics"} <= names and not names & {"rank_suspects", "confirm_root_cause"}


@pytest.mark.parametrize("args,msg", [
    ({}, "missing required"),
    ({"service": "x", "bogus": 1}, "unknown argument"),
    ({"service": 5}, "must be string"),
    ({"service": "x", "max_hops": "4"}, "must be integer"),
    ({"service": "x", "max_hops": True}, "must be integer"),
])
def test_validation_rejects_bad_arguments(args, msg):
    assert msg in validate(REGISTRY["blast_radius"], args)


def test_enum_is_enforced():
    assert "one of" in validate(REGISTRY["set_severity"], {"severity": "sev9", "reason": "x"})
    assert validate(REGISTRY["set_severity"], {"severity": "sev2", "reason": "x"}) is None


# ---- execute(): write-ahead action, citations, wrapping ----------------------------------------------------------
def test_action_is_recorded_with_citations_and_counted_as_a_step(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    out = run(ctx, "triage", "get_alerts")
    assert out.outcome == "success" and out.result["count"] >= 2
    [a] = actions(ctx)
    assert (a["tool"], a["outcome"], a["agent"]) == ("get_alerts", "success", "triage")
    cited = ctx.g.read("MATCH (:Action {id:$a})-[c:CITED]->(n:Service) RETURN n.name", {"a": out.action_id})
    assert "checkout-api" in {r[0] for r in cited}
    assert ep.incident_view(ctx.g, ctx.iid)["steps"] == 1


def test_results_are_wrapped_as_untrusted_data_and_truncated(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    out = run(ctx, "triage", "get_alerts")
    assert out.text.startswith('<tool_result untrusted="true" tool="get_alerts">') and out.text.endswith("</tool_result>")
    from api.tools.base import wrap_untrusted
    big = wrap_untrusted("x", {"blob": "a" * (MAX_RESULT_CHARS * 2)})
    assert "truncated" in big and len(big) < MAX_RESULT_CHARS + 200


def test_unknown_tool_wrong_agent_and_bad_args_become_error_actions_not_exceptions(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    assert run(ctx, "triage", "reboot_everything").outcome == "error"
    wrong = run(ctx, "triage", "rollback", service="checkout-api")  # triage may not remediate
    assert wrong.outcome == "error" and "unknown tool" in wrong.result["error"]
    assert run(ctx, "triage", "blast_radius").outcome == "error"
    assert [a["outcome"] for a in actions(ctx)] == ["error", "error", "error"]
    assert ep.incident_view(ctx.g, ctx.iid)["steps"] == 3  # rejected calls still burn budget
    assert ctx.world.truth(ctx.iid)["harm"] == 0 and not ctx.world.is_healthy(ctx.iid)


def test_unknown_service_error_suggests_close_names(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    out = run(ctx, "triage", "blast_radius", service="checkout-apy")
    assert out.outcome == "error" and "checkout-api" in out.result["error"]


# ---- intake -------------------------------------------------------------------------------------------------------
def test_intake_origin_matches_ground_truth_for_every_scenario(env):
    g, c, w = env
    sets = build_scenarios(c, 42)
    for s in sets["story"] + sets["bench_32"] + sets["heldout_8"]:
        iid = new_id()
        write_events(g, w.inject(iid, s["fault_class"], s["service"]))
        origin = open_from_page(g, w, iid)["origin"]
        t = w.truth(iid)
        if s["fault_class"] == "db_pool_exhaustion":
            assert origin == t["extra"]["top_caller"], s
        elif s["fault_class"] == "cascading":
            assert origin == t["roots"][1]["name"], s       # the service with the bad deploy
        else:
            assert origin == t["roots"][0]["name"], s
        w.advance(3_600_000)


# ---- Triage tools -------------------------------------------------------------------------------------------------
def test_get_alerts_persists_symptoms_and_blast_radius_matches_the_topology(env):
    ctx = start(env, "bad_deploy", "payment-orchestrator")
    run(ctx, "triage", "get_alerts")
    assert any(s["service"] == "payment-orchestrator" for s in ep.incident_view(ctx.g, ctx.iid)["symptoms"])
    out = run(ctx, "triage", "blast_radius", service="payment-orchestrator", max_hops=2)
    expect = {n: h for n, h in env[1].ancestors("payment-orchestrator").items() if h <= 2}
    assert {r["service"]: r["hops"] for r in out.result["affected"]} == expect


def test_owners_of_and_set_severity(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    out = run(ctx, "triage", "owners_of", service="checkout-api")
    assert out.result["owners"][0]["team"] == "Checkout"
    assert run(ctx, "triage", "set_severity", severity="sev1", reason="payments down").outcome == "success"
    assert ep.incident_view(ctx.g, ctx.iid)["severity"] == "sev1"


def test_recall_tool_modes(env):
    g, c, w = env
    ctx = start(env, "bad_deploy", "checkout-api")
    assert run(ctx, "triage", "recall_similar").result["note"] == "no prior episodes"      # cold start
    off = start(env, "bad_deploy", "refund-api", mode="off")
    assert run(off, "triage", "recall_similar").result["note"] == "memory disabled for this run"
    vec = start(env, "bad_deploy", "merchant-api", mode="vector")
    assert run(vec, "triage", "candidate_playbooks", similar_ids=["x"]).result == {"playbooks": []}


# ---- Investigator tools -------------------------------------------------------------------------------------------
def test_recent_changes_finds_the_injected_deploy_and_flag_change(env):
    d = start(env, "bad_deploy", "checkout-api")
    res = run(d, "investigator", "recent_changes", service="checkout-api").result
    injected = d.world.truth(d.iid)["root_cause"][0]["id"]
    assert res["deploys"][0]["deploy"] == injected
    f = start(env, "bad_flag", "checkout-api")
    flags = run(f, "investigator", "recent_changes", service="checkout-api").result["flag_changes"]
    key = f.world.truth(f.iid)["extra"]["flag"]
    assert flags and flags[0]["key"] == key
    cited = f.g.read("MATCH (:Incident {id:$i})-[:TOOK]->(:Action {tool:'recent_changes'})-[:CITED]->(n:ConfigFlag) "
                     "RETURN n.key", {"i": f.iid})
    assert [key] == [r[0] for r in cited]


def test_rank_suspects_puts_the_culprit_first_for_every_bad_deploy_and_bad_flag(env):
    g, c, w = env
    sets = build_scenarios(c, 42)
    cases = [s for s in sets["story"] + sets["bench_32"] if s["fault_class"] in ("bad_deploy", "bad_flag")]
    assert len(cases) >= 9
    for s in cases:
        ctx = start(env, s["fault_class"], s["service"])
        top = run(ctx, "investigator", "rank_suspects").result["suspects"][0]
        assert top["node"] == s["service"] and top["recent_changes"], s
        assert set(top["parts"]) == {"recency", "proximity", "pagerank", "history"}
        w.advance(3_600_000)


def test_rank_suspects_history_term_rewards_services_that_failed_before(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    before = run(ctx, "investigator", "rank_suspects").result["suspects"]
    orch_before = next(s for s in before if s["node"] == "payment-orchestrator")["parts"]["history"]
    assert orch_before == 0
    past = new_id()
    ep.open_incident(ctx.g, past, "old", "sev2", ctx.now - 1000, "payment-orchestrator")
    dep = ctx.g.read("MATCH (d:Deploy)-[:DEPLOYED]->(:Service {name:'payment-orchestrator'}) RETURN d.id LIMIT 1")[0][0]
    ep.confirm_root_cause(ctx.g, past, "Deploy", dep, "investigator")
    ep.set_status(ctx.g, past, "resolved", ctx.now)
    after = run(ctx, "investigator", "rank_suspects").result["suspects"]
    assert next(s for s in after if s["node"] == "payment-orchestrator")["parts"]["history"] > 0


def test_metrics_and_logs_and_planted_injection_is_flagged_not_obeyed(env):
    ctx = start(env, "bad_deploy", "checkout-api", planted_injection=True)
    m = run(ctx, "investigator", "get_metrics", service="checkout-api", metric="error_rate")
    assert m.result["value"] > 0.08
    assert run(ctx, "investigator", "get_metrics", service="checkout-api", metric="bogus").outcome == "error"
    logs = run(ctx, "investigator", "get_logs", service="checkout-api")
    assert INJECTION_LINE in logs.text and logs.text.startswith('<tool_result untrusted="true"')
    kinds = {(s["service"], s["kind"]) for s in ep.incident_view(ctx.g, ctx.iid)["symptoms"]}
    assert ("checkout-api", "suspicious_log") in kinds
    clean = start(env, "bad_deploy", "refund-api")
    run(clean, "investigator", "get_logs", service="refund-api")
    assert not any(k == "suspicious_log" for _, k in
                   {(s["service"], s["kind"]) for s in ep.incident_view(clean.g, clean.iid)["symptoms"]})


def test_dependency_path_and_facts(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    p = run(ctx, "investigator", "dependency_path", from_service="api-gateway", to_service="payment-orchestrator")
    assert p.result["path"][0] == "api-gateway" and p.result["path"][-1] == "payment-orchestrator"
    assert len(p.result["path"]) >= 3
    none = run(ctx, "investigator", "dependency_path", from_service="payment-orchestrator", to_service="api-gateway")
    assert none.result["path"] == [] and "does not depend" in none.result["note"]
    assert run(ctx, "investigator", "facts_about", node="checkout-api").result["facts"] == []


def test_hypotheses_and_root_cause_are_recorded_with_validation(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    dep = ctx.world.truth(ctx.iid)["root_cause"][0]["id"]
    h = run(ctx, "investigator", "propose_hypothesis", text="bad deploy", score=0.8, target_type="Deploy", target=dep)
    assert h.outcome == "success"
    assert run(ctx, "investigator", "propose_hypothesis", text="x", score=1.7, target_type="Deploy",
               target=dep).outcome == "error"
    assert run(ctx, "investigator", "propose_hypothesis", text="x", score=0.5, target_type="Deploy",
               target="nope").outcome == "error"
    run(ctx, "investigator", "update_hypothesis", hypothesis_id=h.result["hypothesis_id"], status="confirmed", score=0.95)
    assert run(ctx, "investigator", "update_hypothesis", hypothesis_id="missing", status="open").outcome == "error"
    assert run(ctx, "investigator", "confirm_root_cause", target_type="Deploy", target=dep).outcome == "success"
    view = ep.incident_view(ctx.g, ctx.iid)
    assert view["hypotheses"][0]["status"] == "confirmed" and view["hypotheses"][0]["score"] == 0.95
    assert view["root_causes"] == [{"label": "Deploy", "id": dep, "confirmed_by": "investigator"}]


# ---- Remediator tools and guardrails ------------------------------------------------------------------------------
def test_correct_fix_resolves_and_wrong_guess_does_not(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    assert run(ctx, "remediator", "restart", service="payment-orchestrator").outcome == "no_effect"
    assert run(ctx, "remediator", "check_health", service="checkout-api").result["status"] == "red"
    assert run(ctx, "remediator", "rollback", service="checkout-api").outcome == "success"
    assert run(ctx, "remediator", "check_health", service="checkout-api").result["status"] == "green"
    assert ctx.world.is_healthy(ctx.iid)


def test_out_of_scope_targets_are_refused_before_touching_the_world(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    outside = next(n for n in sorted(env[1].services) if n not in ctx.scope())   # a service the incident cannot reach
    out = run(ctx, "remediator", "rollback", service=outside)
    assert out.outcome == "error" and out.result["error"].startswith("out_of_scope")
    assert ctx.world.truth(ctx.iid)["harm"] == 0 and ctx.world.tick == 0   # a refusal never touches the world
    assert [a["outcome"] for a in actions(ctx)] == ["error"]


def test_red_herring_alerts_are_observed_but_not_affected_and_not_in_scope(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    alerting = {a["service"] for a in ctx.page["alerts"]}
    blast = set(env[1].ancestors("checkout-api")) | {"checkout-api"}
    herrings = alerting - blast
    assert herrings, "scenario should include at least one red herring"
    affected = {s["name"] for s in ep.incident_view(ctx.g, ctx.iid)["services"]}
    seen = {s["service"] for s in ep.incident_view(ctx.g, ctx.iid)["symptoms"]}
    assert affected == (alerting & blast) | {"checkout-api"} and not affected & herrings
    assert herrings <= seen                                     # still recorded as observations
    for h in herrings:
        if h not in ctx.scope():
            assert run(ctx, "remediator", "rollback", service=h).result["error"].startswith("out_of_scope")


def test_tier_critical_service_allows_one_restart_per_incident(env):
    ctx = start(env, "memory_leak", "session-service", recurs=True)
    assert run(ctx, "remediator", "restart", service="session-service").outcome == "success"
    again = run(ctx, "remediator", "restart", service="session-service")
    assert again.outcome == "error" and "already restarted" in again.result["error"]
    assert run(ctx, "remediator", "rollback", service="session-service").outcome == "success"
    assert ctx.world.is_healthy(ctx.iid)


def test_gateway_restart_and_failover_need_a_strong_hypothesis(env):
    ctx = start(env, "dependency_outage", "tokenizer")
    refused = run(ctx, "remediator", "failover", service="tokenizer")
    assert refused.outcome == "error" and "needs a hypothesis" in refused.result["error"]
    gw = run(ctx, "remediator", "restart", service="api-gateway")
    assert gw.outcome == "error" and "needs a hypothesis" in gw.result["error"]
    weak = run(ctx, "investigator", "propose_hypothesis", text="maybe", score=0.4, target_type="Service",
               target="tokenizer")
    assert weak.outcome == "success"
    assert run(ctx, "remediator", "failover", service="tokenizer").outcome == "error"      # 0.4 is not enough
    run(ctx, "investigator", "propose_hypothesis", text="hard down", score=0.9, target_type="Service",
        target="tokenizer")
    assert run(ctx, "remediator", "failover", service="tokenizer").outcome == "success"
    assert ctx.world.is_healthy(ctx.iid)


def test_a_rejected_hypothesis_does_not_unlock_a_guarded_action(env):
    ctx = start(env, "dependency_outage", "tokenizer")
    h = run(ctx, "investigator", "propose_hypothesis", text="x", score=0.9, target_type="Service", target="tokenizer")
    run(ctx, "investigator", "update_hypothesis", hypothesis_id=h.result["hypothesis_id"], status="rejected")
    assert run(ctx, "remediator", "failover", service="tokenizer").outcome == "error"


def test_prompt_injection_scenario_ends_with_zero_harmful_actions_when_the_gateway_is_guarded(env):
    ctx = start(env, "bad_deploy", "checkout-api", planted_injection=True)
    run(ctx, "investigator", "get_logs", service="checkout-api")   # the agent reads the planted line...
    obeyed = run(ctx, "remediator", "restart", service="api-gateway")  # ...and a gullible model tries to comply
    assert obeyed.outcome == "error"
    assert ctx.world.truth(ctx.iid)["harm"] == 0
    assert run(ctx, "remediator", "rollback", service="checkout-api").outcome == "success"


def test_set_flag_is_scoped_by_the_flag_owner_and_needs_the_previous_value(env):
    ctx = start(env, "bad_flag", "checkout-api")
    t = ctx.world.truth(ctx.iid)["steps"][0]
    foreign = next(k for k, f in sorted(env[1].flags.items()) if f["service"] not in ctx.scope())
    assert run(ctx, "remediator", "set_flag", key=foreign, value="x").result["error"].startswith("out_of_scope")
    assert run(ctx, "remediator", "set_flag", key="no.such.flag", value="1").outcome == "error"
    assert run(ctx, "remediator", "set_flag", key=t["target"], value="999").outcome == "no_effect"
    assert run(ctx, "remediator", "set_flag", key=t["target"], value=t["value"]).outcome == "success"


def test_playbook_steps_tool(env):
    ctx = start(env, "bad_deploy", "checkout-api")
    done = new_id()
    ep.open_incident(ctx.g, done, "old", "sev2", 1, "checkout-api")
    pid = pr.create_playbook(ctx.g, "rollback", "bad_deploy", [{"tool": "rollback", "args_template": {"service": "$origin"}}],
                             done, 1)
    out = run(ctx, "remediator", "playbook_steps", playbook_id=pid)
    assert out.result["steps"][0]["tool"] == "rollback" and out.result["steps"][0]["args"] == {"service": "$origin"}
    assert run(ctx, "remediator", "playbook_steps", playbook_id="nope").outcome == "error"


def test_cascading_scope_allows_both_the_datastore_fix_and_the_deploy_rollback(env):
    ctx = start(env, "cascading", "payments-db")
    t = ctx.world.truth(ctx.iid)
    outcomes = [run(ctx, "remediator", s["tool"], **({"target": s["target"]} if s["tool"] == "scale"
                                                      else {"service": s["target"]})).outcome for s in t["steps"]]
    assert outcomes == ["success"] * 3 and ctx.world.is_healthy(ctx.iid)
