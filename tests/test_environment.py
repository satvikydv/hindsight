"""Regression tests for defects the offline benchmark surfaced. Each test names the failure it prevents."""

import pytest
from ulid import ULID

from api import config
from api.agents.context import MAX_SYMPTOMS, build_context
from api.agents.coordinator import Coordinator
from api.agents.intake import open_from_page, strength
from api.bench.scripted import Ctx, expert_llm
from api.graph import queries as q
from api.graph.client import TenantGraph
from api.graph.schema import init_graph
from api.ids import new_id
from api.llm import FakeEmbedder
from api.memory import consolidate as cons
from api.memory import episodic as ep
from api.memory import recall as rc
from api.sim.generator import SIM_NOW, seed_tenant
from api.sim.world import World, write_events
from api.tools import ToolContext

DAY = 86_400_000


@pytest.fixture
def env():
    g = TenantGraph("test_" + str(ULID()).lower()[-8:])
    init_graph(g, embed_dim=64)
    c = seed_tenant(g, 42)
    yield g, c, World(c, 1)
    g.delete()


def inject(env, fault_class, service, **kw):
    g, c, w = env
    iid = new_id()
    write_events(g, w.inject(iid, fault_class, service, **kw))
    open_from_page(g, w, iid)
    return iid


def recent(g, w, svc):
    return g.rows(q.Q2_RECENT_CHANGES, {"svc": svc, "since": w.now_ms - 6 * 3_600_000})


def flags(g, w, names):
    return g.rows(q.Q21_RECENT_FLAG_CHANGES, {"names": names, "since": w.now_ms - 6 * 3_600_000})


def run_crew(env, fault_class, service, mode="graph", max_steps=20, **kw):
    g, c, w = env
    co = Coordinator(g, w, expert_llm(), FakeEmbedder(64), mode, max_steps=max_steps)
    res = co.run(co.launch(fault_class, service, **kw))
    w.advance(3_600_000)
    return res


# ---- reverts are written to the graph (stale evidence made later incidents chase ghosts) -------------------------
def test_a_rolled_back_deploy_stops_being_a_recent_change(env):
    g, c, w = env
    res = run_crew(env, "bad_deploy", "checkout-api")
    assert res.status == "resolved"
    assert recent(g, w, "checkout-api") == []                                   # reverted: no longer live evidence
    d = g.read("MATCH (d:Deploy) WHERE d.source = 'sim' RETURN d.reverted_at, d.version")
    assert len(d) == 1 and d[0][0] is not None                                   # but kept, marked, never deleted


def test_a_restored_flag_stops_being_a_recent_change_and_history_is_kept(env):
    g, c, w = env
    run_crew(env, "bad_flag", "checkout-api")
    assert flags(g, w, ["checkout-api"]) == []
    key = [k for k, f in c.flags.items() if f["service"] == "checkout-api"]
    values = g.read("MATCH (:Service {name:'checkout-api'})-[h:HAS_FLAG]->(f:ConfigFlag) WHERE f.key IN $k "
                    "RETURN f.key, f.value, h.valid_to IS NULL ORDER BY f.created_at", {"k": key})
    current = [v for v in values if v[2]]
    assert len(current) == len(key) and all(w.flag_values[k] == v for k, v, _ in current)   # graph matches the world
    assert any(not v[2] for v in values)                                         # the bad value is still in history


def test_a_failed_incident_is_cleaned_up_by_humans_and_does_not_poison_the_next_one(env):
    g, c, w = env
    res = run_crew(env, "bad_deploy", "checkout-api", max_steps=4)               # starved of steps: it fails
    assert res.status == "failed"
    assert recent(g, w, "checkout-api") == []                                    # settle() reverted the leftover deploy
    assert w.is_healthy(res.incident_id)                                         # and cleared the fault
    nxt = run_crew(env, "bad_deploy", "refund-api")
    assert nxt.status == "resolved" and nxt.harmful_actions == 0


def test_a_self_healed_incident_does_not_leave_a_misleading_flag_behind(env):
    g, c, w = env
    res = run_crew(env, "bad_flag", "ledger-writer", self_heal=True)
    assert res.reason == "self_healed"
    assert flags(g, w, ["ledger-writer"]) == []
    key = next(k for k, f in c.flags.items() if f["service"] == "ledger-writer")
    assert w.flag_values[key] == c.flags[key]["value"]                           # the config really was restored


def test_settle_is_idempotent_and_survives_a_snapshot_roundtrip(env):
    g, c, w = env
    iid = inject(env, "bad_deploy", "checkout-api")
    first = w.settle(iid)
    assert [e["type"] for e in first] == ["deploy_reverted"] and w.settle(iid) == []
    again = World.restore(c, w.snapshot())
    assert again.settle(iid) == []                                               # "reverted" survives the round trip


def test_a_revert_event_is_only_emitted_for_the_injected_change(env):
    """A rollback of a service that merely has an old deploy must not mark that old deploy as reverted."""
    g, c, w = env
    iid = inject(env, "memory_leak", "session-service", recurs=True)
    ctx = ToolContext(g, w, iid, "graph")
    from api.tools import execute
    execute(ctx, "remediator", "restart", {"service": "session-service"}, 1)
    out = execute(ctx, "remediator", "rollback", {"service": "session-service"}, 2)
    assert out.outcome == "success"
    assert g.read("MATCH (d:Deploy) WHERE d.reverted_at IS NOT NULL RETURN count(d)") == [[0]]


# ---- context: the loudest symptoms survive truncation -------------------------------------------------------------
def test_symptom_truncation_keeps_the_loudest_not_the_alphabetically_first(env):
    g, c, w = env
    iid = new_id()
    ep.open_incident(g, iid, "many symptoms", "sev2", w.now_ms, "checkout-api")
    quiet = sorted(c.services)[:MAX_SYMPTOMS + 5]                               # alphabetical names, all mild
    loud = "tokenizer"
    assert loud not in quiet and loud == sorted(c.services)[-1]                  # sorts last by name: cut by a name sort
    ep.add_symptoms(g, iid, [{"service": s, "kind": "error_rate", "metric": "error_rate", "value": 0.09} for s in quiet])
    ep.add_symptoms(g, iid, [{"service": loud, "kind": "latency", "metric": "latency_p99_ms", "value": 1020}])
    shown = Ctx(build_context(ToolContext(g, w, iid, "graph"), "remediator")).symptoms
    assert loud in shown                                                         # kept despite sorting last by name
    assert sum(len(v) for v in shown.values()) <= MAX_SYMPTOMS and len(shown) < len(quiet) + 1   # a mild one went


def test_strength_ranks_local_signals_and_survives_symptoms_without_a_threshold():
    assert strength({"kind": "crashloop", "metric": "restarts", "value": 8}) > strength(
        {"kind": "error_rate", "metric": "error_rate", "value": 0.9})            # local signals point at the origin
    assert strength({"kind": "suspicious_log", "metric": "log", "value": 1}) == 1.0
    assert strength({"kind": "error_rate", "metric": "error_rate", "value": None}) == 1.0


# ---- learning: playbooks must transfer ----------------------------------------------------------------------------
def test_a_connection_pool_fix_is_stored_with_a_top_caller_placeholder_not_a_service_name(env):
    g, c, w = env
    res = run_crew(env, "db_pool_exhaustion", "payments-db")
    assert res.status == "resolved"
    [pb] = g.rows("MATCH (p:Playbook) RETURN p.id AS id")
    from api.memory import procedural as pr
    steps = pr.steps_of(g, pb["id"])
    assert [(s["tool"], s["args"]) for s in steps] == [("scale", {"target": "$datastore"}),
                                                       ("restart", {"service": "$top_caller"})]


def test_templatize_uses_top_caller_only_for_restarts():
    t = cons.templatize
    assert t({"tool": "restart", "args": {"service": "metrics-collector"}}, "sms-sender", {"risk-db"},
             "metrics-collector")["args_template"] == {"service": "$top_caller"}
    assert t({"tool": "rollback", "args": {"service": "metrics-collector"}}, "metrics-collector", {"risk-db"},
             "metrics-collector")["args_template"] == {"service": "$origin"}     # a rollback of the origin stays $origin
    assert t({"tool": "restart", "args": {"service": "x"}}, "x", set(), None)["args_template"] == {"service": "$origin"}


# ---- recall: triage-time signals tell fault classes apart --------------------------------------------------------
def signals_of(env, fault_class, service, **kw):
    g, c, w = env
    iid = inject(env, fault_class, service, **kw)
    return iid, g.read("MATCH (i:Incident {id:$i}) RETURN i.signals", {"i": iid})[0][0]


@pytest.mark.parametrize("fault_class,service,expected", [
    ("bad_deploy", "checkout-api", ["deploy"]), ("bad_flag", "checkout-api", ["flag"]),
    ("db_pool_exhaustion", "payments-db", ["saturation"]), ("memory_leak", "session-service", ["crashloop"]),
    ("cert_expiry", "tokenizer", ["cert"]), ("dependency_outage", "tokenizer", []), ("traffic_spike", "login-api", []),
    ("cascading", "payments-db", ["deploy", "saturation"])])
def test_signals_identify_the_kind_of_failure_before_anyone_investigates(env, fault_class, service, expected):
    assert signals_of(env, fault_class, service)[1] == expected


def test_structural_similarity_separates_fault_classes_at_the_fast_path_gate(env):
    """A bad deploy and a hard-down dependency both show error_rate; only structure tells them apart."""
    g, c, w = env
    run_crew(env, "bad_deploy", "checkout-api")                                  # one resolved bad deploy in memory
    gate = config.FAST_PATH_MIN_STRUCTURE

    def structural(fault_class, service, **kw):
        iid = inject(env, fault_class, service, **kw)
        found = rc.recall_similar(g, FakeEmbedder(64), iid, w.now_ms, "graph").similar
        return max((s["parts"]["structural"] for s in found), default=0.0)

    same_class_other_domain = structural("bad_deploy", "login-api")              # identity, not payments
    assert same_class_other_domain >= gate
    for fault_class, service in [("dependency_outage", "tokenizer"), ("bad_flag", "refund-api"),
                                 ("traffic_spike", "merchant-api"), ("memory_leak", "payout-service"),
                                 ("cert_expiry", "profile-service")]:
        assert structural(fault_class, service) < gate, fault_class


def test_the_fast_path_gate_is_derived_from_the_weights_not_fitted():
    assert rc.S_JACCARD + rc.S_SIGNALS == pytest.approx(0.70)                    # both class features matching
    assert config.FAST_PATH_MIN_STRUCTURE < rc.S_JACCARD + rc.S_SIGNALS          # ...clears the gate
    assert rc.S_JACCARD + rc.S_DOMAIN + rc.S_TIER + rc.S_NEIGHBOURS < config.FAST_PATH_MIN_STRUCTURE  # one feature does not
    assert rc.S_SIGNALS + rc.S_DOMAIN + rc.S_TIER + rc.S_NEIGHBOURS < config.FAST_PATH_MIN_STRUCTURE
    assert sum([rc.S_JACCARD, rc.S_SIGNALS, rc.S_DOMAIN, rc.S_TIER, rc.S_NEIGHBOURS]) == pytest.approx(1.0)


def test_fast_path_prefers_the_best_structural_match_over_the_heaviest_playbook(env):
    g, c, w = env
    run_crew(env, "bad_deploy", "checkout-api")                                  # bad_deploy: rollback (weight 0.67)
    for svc in ("refund-api", "merchant-api", "payout-service"):                 # reinforce it a few times
        run_crew(env, "bad_deploy", svc)
    run_crew(env, "dependency_outage", "tokenizer")                              # a different playbook
    heavy = max(g.read("MATCH (p:Playbook) RETURN p.weight"))[0]
    res = run_crew(env, "bad_deploy", "invoice-service")
    assert res.fast_path and res.steps <= 6 and res.harmful_actions == 0 and heavy > 0.67


# ---- the policy reads what the crew already did (recurring memory leak) ------------------------------------------
def test_a_recurring_leak_after_a_one_step_playbook_still_resolves_through_the_bounce(env):
    """Found by the benchmark: [restart] playbook + a leak that recurs. The next turn must see the restart in the
    context's 'actions so far' instead of restarting forever."""
    g, c, w = env
    run_crew(env, "memory_leak", "payout-service", recurs=False)                 # learns [restart]
    res = run_crew(env, "memory_leak", "account-api", recurs=True)               # needs restart THEN rollback
    assert res.status == "resolved" and res.harmful_actions == 0 and res.steps < 20
    again = run_crew(env, "memory_leak", "metrics-collector", recurs=True)       # and the same again with what it learned
    assert again.status == "resolved"
