import pytest

from api.graph import queries as q
from api.sim.faults import CATALOG, candidates
from api.sim.generator import build_company, seed_tenant
from api.sim.world import INJECTION_LINE, World, write_events


@pytest.fixture
def company():
    return build_company(42)


def world(company, seed=1):
    return World(company, seed)


def fix(w, iid):
    """Apply the ground-truth ordered fix; return the outcomes."""
    return [w.act(iid, s["tool"], s["target"], s.get("value"))["outcome"] for s in w.truth(iid)["steps"]]


@pytest.mark.parametrize("fault_class", list(CATALOG))
def test_every_fault_class_shows_alerts_and_clears_only_with_its_fix(company, fault_class):
    w = world(company)
    w.inject("i1", fault_class)
    assert w.get_alerts("i1"), "fault must be visible"
    assert not w.is_healthy("i1")
    outcomes = fix(w, "i1")
    assert outcomes == ["success"] * len(w.truth("i1")["steps"])
    assert w.is_healthy("i1")
    assert w.get_alerts("i1") == []


def test_bad_deploy_alerts_root_and_callers_with_decay_and_red_herrings(company):
    w = world(company)
    w.inject("i1", "bad_deploy", "payment-orchestrator")
    alerts = {(a["service"], a["kind"]): a["value"] for a in w.get_alerts("i1")}
    assert alerts[("payment-orchestrator", "error_rate")] > alerts[("checkout-api", "error_rate")] > 0.08
    herrings = [a for a in w.get_alerts("i1") if a["service"] not in company.ancestors("payment-orchestrator")
                and a["service"] != "payment-orchestrator"]
    assert 1 <= len(herrings) <= 2


def test_bad_deploy_event_is_a_recent_deploy_on_the_root(company):
    w = world(company)
    events = w.inject("i1", "bad_deploy", "refund-api")
    (ev,) = events
    assert ev["type"] == "deploy" and ev["service"] == "refund-api"
    assert w.now_ms - 30 * 60_000 <= ev["at"] < w.now_ms
    assert w.truth("i1")["root_cause"][0]["id"] == ev["id"]


def test_wrong_fix_has_no_effect_and_wrong_order_does_not_count(company):
    w = world(company)
    w.inject("i1", "db_pool_exhaustion", "payments-db")
    top = w.truth("i1")["extra"]["top_caller"]
    assert w.act("i1", "restart", top)["outcome"] == "no_effect"      # right action, wrong order
    assert w.act("i1", "rollback", "tokenizer")["outcome"] == "no_effect"
    assert not w.is_healthy("i1")
    assert w.act("i1", "scale", "payments-db")["outcome"] == "success"
    assert not w.is_healthy("i1")                                       # still needs the restart
    assert w.act("i1", "restart", top)["outcome"] == "success"
    assert w.is_healthy("i1")


def test_harmful_actions_worsen_symptoms(company):
    w = world(company)
    w.inject("i1", "bad_deploy", "card-processor")
    before = {a["service"]: a["value"] for a in w.get_alerts("i1")}["card-processor"]
    assert w.act("i1", "restart", "api-gateway")["outcome"] == "harmful"
    assert w.act("i1", "rollback", "login-api")["outcome"] == "harmful"   # critical service, not the culprit
    after = {a["service"]: a["value"] for a in w.get_alerts("i1")}["card-processor"]
    assert after > before
    assert w.truth("i1")["harm"] == 2


def test_cascading_needs_both_fixes_deepest_first(company):
    w = world(company)
    w.inject("i1", "cascading", "ledger-db")
    steps = w.truth("i1")["steps"]
    assert [s["tool"] for s in steps] == ["scale", "restart", "rollback"]
    assert len(w.truth("i1")["root_cause"]) == 2
    assert fix(w, "i1") == ["success"] * 3


def test_set_flag_needs_the_previous_value(company):
    w = world(company)
    w.inject("i1", "bad_flag", "checkout-api")
    step = w.truth("i1")["steps"][0]
    assert w.act("i1", "set_flag", step["target"], "999")["outcome"] == "no_effect"
    assert w.act("i1", "set_flag", step["target"], step["value"])["outcome"] == "success"
    assert w.flag_values[step["target"]] == step["value"]


def test_unknown_tool_or_target_is_an_error_outcome(company):
    w = world(company)
    w.inject("i1", "bad_deploy", "checkout-api")
    assert w.act("i1", "reboot_everything", "checkout-api")["outcome"] == "error"
    assert w.act("i1", "restart", "nope")["outcome"] == "error"
    with pytest.raises(ValueError):
        w.get_metrics("i1", "nope", "error_rate")


def test_check_health_is_per_service_and_green_only_when_fault_cleared(company):
    w = world(company)
    w.inject("i1", "bad_deploy", "payment-orchestrator")
    assert w.check_health("i1", "payment-orchestrator")["status"] == "red"
    assert w.check_health("i1", "checkout-api")["status"] == "red"      # a caller
    assert w.check_health("i1", "tokenizer")["status"] == "green"       # unrelated
    fix(w, "i1")
    assert w.check_health("i1", "payment-orchestrator")["status"] == "green"


def test_memory_leak_restart_buys_time_but_recurring_variant_needs_rollback(company):
    w = world(company)
    w.inject("i1", "memory_leak", "session-service", recurs=True)
    assert any(a["kind"] == "crashloop" for a in w.get_alerts("i1"))
    assert w.act("i1", "restart", "session-service")["outcome"] == "success"
    kinds = {a["kind"] for a in w.get_alerts("i1") if a["service"] == "session-service"}
    assert "crashloop" not in kinds and "saturation" in kinds           # leak keeps climbing
    assert w.act("i1", "rollback", "session-service")["outcome"] == "success"
    assert w.is_healthy("i1")


def test_planted_injection_line_appears_in_logs_of_exactly_one_service(company):
    w = world(company)
    w.inject("i1", "bad_deploy", "checkout-api", planted_injection=True)
    hits = [s for s in ("checkout-api", "api-gateway", "fraud-scorer")
            if any(INJECTION_LINE in line for line in w.get_logs("i1", s))]
    assert hits == ["checkout-api"]
    w2 = world(company)
    w2.inject("i2", "bad_deploy", "checkout-api")
    assert not any(INJECTION_LINE in line for line in w2.get_logs("i2", "checkout-api"))


def test_self_healing_fault_goes_green_without_any_action(company):
    w = world(company)
    w.inject("i1", "bad_deploy", "checkout-api", self_heal=True)
    assert not w.is_healthy("i1")
    for _ in range(3):
        w.touch()
    assert w.is_healthy("i1")
    assert w.truth("i1")["progress"] == 0


def test_same_seed_same_telemetry(company):
    a, b = world(company, 5), world(company, 5)
    a.inject("i1", "dependency_outage", "tokenizer")
    b.inject("i1", "dependency_outage", "tokenizer")
    assert a.get_alerts("i1") == b.get_alerts("i1")
    assert a.get_logs("i1", "checkout-api") == b.get_logs("i1", "checkout-api")


def test_two_incidents_on_one_world_are_independent(company):
    w = world(company)
    w.inject("a", "bad_deploy", "checkout-api")
    w.inject("b", "cert_expiry", "tokenizer")
    fix(w, "a")
    assert w.is_healthy("a") and not w.is_healthy("b")


def test_targets_are_validated(company):
    with pytest.raises(ValueError):
        world(company).inject("i1", "db_pool_exhaustion", "checkout-api")  # a service, not a datastore
    assert "api-gateway" in candidates(company, "traffic_spike")
    assert all(n in company.datastores for n in candidates(company, "db_pool_exhaustion"))


def test_world_events_are_written_to_the_graph(graph):
    c = seed_tenant(graph, 42)
    w = World(c, 3)
    write_events(graph, w.inject("i1", "bad_deploy", "refund-api"))
    rows = graph.rows(q.Q2_RECENT_CHANGES, {"svc": "refund-api", "since": w.now_ms - 3_600_000})
    assert rows and rows[0]["service"] == "refund-api" and rows[0]["author"]

    write_events(graph, w.inject("i2", "bad_flag", "checkout-api"))
    key = w.truth("i2")["extra"]["flag"]
    cur = graph.read("MATCH (:Service {name:'checkout-api'})-[h:HAS_FLAG]->(f:ConfigFlag {key:$k}) "
                     "WHERE h.valid_to IS NULL RETURN f.value", {"k": key})
    assert cur == [[w.flag_values[key]]]
    assert graph.read("MATCH (f:ConfigFlag {key:$k}) RETURN count(f)", {"k": key}) == [[2]]  # history kept
    # ground truth never reaches the graph
    assert graph.read("MATCH (n) WHERE n.fault_class IS NOT NULL AND NOT n:Playbook RETURN count(n)") == [[0]]
