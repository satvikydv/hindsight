import pytest
from ulid import ULID

from api.graph.client import TenantGraph
from api.graph.schema import init_graph
from api.ids import new_id
from api.llm import FakeEmbedder
from api.memory import consolidate as cons
from api.memory import episodic as ep
from api.memory import procedural as pr
from api.sim.generator import SIM_NOW, seed_tenant

DATASTORES = {"payments-db", "ledger-db"}


@pytest.fixture
def g():
    g = TenantGraph("test_" + str(ULID()).lower()[-8:])
    init_graph(g, embed_dim=64)
    seed_tenant(g, 42)
    yield g
    g.delete()


def incident(g, origin, actions, status="resolved", cause=None, followed=None):
    """actions: [(tool, args, outcome)] by the remediator, in order."""
    iid = new_id()
    ep.open_incident(g, iid, f"{origin} incident", "sev2", SIM_NOW, origin)
    for n, (tool, args, outcome) in enumerate(actions, 1):
        aid = new_id()
        ep.begin_action(g, iid, aid, "remediator", tool, args, n, SIM_NOW + n)
        ep.finish_action(g, aid, outcome, outcome, SIM_NOW + n)
    if cause:
        ep.confirm_root_cause(g, iid, *cause, "investigator")
    if followed:
        ep.set_fast_path(g, iid, followed)
    ep.set_status(g, iid, status, SIM_NOW + 100)
    return iid


def deploy_of(g, service):
    return g.read("MATCH (d:Deploy)-[:DEPLOYED]->(:Service {name:$s}) RETURN d.id LIMIT 1", {"s": service})[0][0]


def run(g, iid):
    return cons.consolidate(g, FakeEmbedder(64), iid, SIM_NOW + 200)


@pytest.mark.parametrize("tools,first_target,expected", [
    (["rollback"], None, "bad_deploy"),
    (["set_flag"], None, "bad_flag"),
    (["scale", "restart"], "payments-db", "db_pool_exhaustion"),
    (["scale", "restart", "rollback"], "payments-db", "cascading"),
    (["scale", "restart"], "checkout-api", "other:scale+restart"),      # scaling a service then restarting: not db pool
    (["restart"], None, "memory_leak"),
    (["restart", "rollback"], None, "memory_leak"),
    (["renew_cert"], None, "cert_expiry"),
    (["failover"], None, "dependency_outage"),
    (["scale"], "api-gateway", "traffic_spike"),
    (["rollback", "restart"], None, "other:rollback+restart"),
    ([], None, "other:"),
])
def test_fault_class_is_inferred_from_the_fix_that_worked(tools, first_target, expected):
    seq = [{"tool": t, "args": {"target": first_target} if t == "scale" and n == 0 else {"service": "x"}}
           for n, t in enumerate(tools)]
    assert cons.infer_fault_class(seq, DATASTORES) == expected


def test_templatize_replaces_names_with_placeholders_that_transfer_between_services():
    t = cons.templatize
    assert t({"tool": "rollback", "args": {"service": "checkout-api"}}, "checkout-api", DATASTORES) == {
        "tool": "rollback", "args_template": {"service": "$origin"}}
    assert t({"tool": "scale", "args": {"target": "payments-db"}}, "checkout-api", DATASTORES)["args_template"] == {
        "target": "$datastore"}
    assert t({"tool": "set_flag", "args": {"key": "checkout.retry_limit", "value": "3"}}, "checkout-api",
             DATASTORES)["args_template"] == {"key": "$changed_flag", "value": "$previous_value"}
    assert t({"tool": "restart", "args": {"service": "tokenizer"}}, "checkout-api", DATASTORES)["args_template"] == {
        "service": "tokenizer"}                                          # unrelated names stay literal


def test_only_successful_remediation_counts_as_the_winning_sequence(g):
    iid = incident(g, "checkout-api", [("restart", {"service": "payment-orchestrator"}, "no_effect"),
                                       ("rollback", {"service": "login-api"}, "harmful"),
                                       ("rollback", {"service": "checkout-api"}, "success"),
                                       ("rollback", {"service": "refund-api"}, "error")])
    assert [(s["tool"], s["args"]["service"]) for s in cons.winning_sequence(ep.incident_view(g, iid))] == [
        ("rollback", "checkout-api")]


def test_first_fix_creates_a_playbook_and_an_identical_later_fix_merges_into_it(g):
    a = incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")],
                 cause=("Deploy", deploy_of(g, "checkout-api")))
    assert run(g, a)["playbook_action"] == "created"
    b = incident(g, "merchant-api", [("rollback", {"service": "merchant-api"}, "success")],
                 cause=("Deploy", deploy_of(g, "merchant-api")))
    assert run(g, b)["playbook_action"] == "merged"
    [pb] = pr.list_playbooks(g)
    assert (pb["attempts"], pb["successes"]) == (2, 2)
    other = incident(g, "login-api", [("restart", {"service": "login-api"}, "success")])
    assert run(g, other)["playbook_action"] == "created"                 # different fix: a second playbook
    assert len(pr.list_playbooks(g)) == 2


def test_a_followed_playbook_that_worked_is_reinforced_step_by_step(g):
    a = incident(g, "checkout-api", [("scale", {"target": "payments-db"}, "success"),
                                     ("restart", {"service": "checkout-api"}, "success")])
    pid = run(g, a)["playbook"]
    b = incident(g, "refund-api", [("scale", {"target": "payments-db"}, "success"),
                                   ("restart", {"service": "refund-api"}, "success")], followed=pid)
    rep = run(g, b)
    assert rep["playbook_action"] == "reinforced" and rep["playbook"] == pid
    assert {s["step"]: (s["wins"], s["tries"]) for s in pr.steps_of(g, pid)} == {1: (2, 2), 2: (2, 2)}


def test_a_followed_playbook_that_was_not_what_worked_is_penalised_and_the_real_fix_is_learned(g):
    a = incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")])
    pid = run(g, a)["playbook"]
    b = incident(g, "login-api", [("rollback", {"service": "login-api"}, "no_effect"),
                                  ("restart", {"service": "login-api"}, "success")], followed=pid)
    rep = run(g, b)
    assert rep["fault_class"] == "memory_leak" and rep["playbook_action"] == "created" and rep["playbook"] != pid
    old = next(p for p in pr.list_playbooks(g) if p["id"] == pid)
    assert (old["attempts"], old["successes"]) == (2, 1)                 # tried, did not carry the incident


def test_a_declined_playbook_is_not_counted_as_an_attempt_but_the_real_fix_is_still_learned(g):
    pid = run(g, incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")]))["playbook"]
    # fast path chosen; the Remediator found no recent deploy and declined; the Investigator found the real fix
    b = incident(g, "fraud-scorer", [("set_flag", {"key": "risk.strict_mode", "value": "false"}, "success")],
                 followed=pid)
    ep.decline_fast_path(g, b)
    rep = run(g, b)
    assert rep["followed_playbook"] is None and rep["playbook_action"] == "created"
    assert rep["fault_class"] == "bad_flag"
    old = next(p for p in pr.list_playbooks(g) if p["id"] == pid)
    assert (old["attempts"], old["successes"], old["status"]) == (1, 1, "active")     # untouched
    assert g.read("MATCH (i:Incident {id:$i}) RETURN i.fast_path_playbook, i.fast_path_declined", {"i": b}) == [
        [None, pid]]


def test_a_playbook_that_was_tried_and_failed_is_penalised_even_if_the_incident_was_later_fixed_differently(g):
    pid = run(g, incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")]))["playbook"]
    b = incident(g, "fraud-scorer", [("rollback", {"service": "fraud-scorer"}, "harmful"),
                                     ("set_flag", {"key": "risk.strict_mode", "value": "false"}, "success")],
                 followed=pid)                                  # no decline: the rollback really was attempted
    run(g, b)
    old = next(p for p in pr.list_playbooks(g) if p["id"] == pid)
    assert (old["attempts"], old["successes"]) == (2, 1)


def test_a_playbook_the_remediator_bypassed_after_checking_evidence_is_not_penalised(g):
    """Found live: the Remediator saw a flag change instead of a deploy and fixed it with set_flag in the same turn. The
    rollback playbook was never tried, so it must stay untouched (no explicit decline was recorded)."""
    pid = run(g, incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")]))["playbook"]
    b = incident(g, "fraud-scorer", [("set_flag", {"key": "risk.strict_mode", "value": "false"}, "success")],
                 followed=pid)
    rep = run(g, b)
    assert "not tried" in rep["playbook_note"] and rep["playbook_action"] == "created" and rep["fault_class"] == "bad_flag"
    old = next(p for p in pr.list_playbooks(g) if p["id"] == pid)
    assert (old["attempts"], old["successes"], old["status"]) == (1, 1, "active")

def test_a_playbook_named_but_never_acted_on_is_not_tried(g):
    pid = run(g, incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")]))["playbook"]
    b = incident(g, "refund-api", [], status="failed", followed=pid)
    rep = run(g, b)
    assert "not tried" in rep["playbook_note"] and rep["playbook_action"] == "none"
    assert next(p for p in pr.list_playbooks(g) if p["id"] == pid)["attempts"] == 1

def test_a_failed_incident_lowers_the_followed_playbook_and_creates_nothing(g):
    a = incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")])
    pid = run(g, a)["playbook"]
    b = incident(g, "refund-api", [("rollback", {"service": "refund-api"}, "no_effect")], status="failed",
                 followed=pid)
    rep = run(g, b)
    assert rep["playbook_action"] == "none" and rep["lessons"] == 0
    [pb] = pr.list_playbooks(g)
    assert (pb["attempts"], pb["successes"]) == (2, 1) and pb["weight"] == pytest.approx(2 / 4)


def test_repeated_failures_eventually_deprecate_the_playbook(g):
    pid = run(g, incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")]))["playbook"]
    for _ in range(3):   # the playbook really is tried each time (a state-changing action ran) and does not help
        run(g, incident(g, "refund-api", [("rollback", {"service": "refund-api"}, "no_effect")], status="failed",
                        followed=pid))
    [pb] = pr.list_playbooks(g)
    assert pb["status"] == "deprecated" and pb["attempts"] == 4


def test_nothing_is_learned_from_a_self_healed_incident(g):
    rep = run(g, incident(g, "checkout-api", []))
    assert "self_healed" in rep["note"] and pr.list_playbooks(g) == []


def test_lessons_are_capped_written_as_facts_and_not_duplicated_on_repeat(g):
    dep = deploy_of(g, "checkout-api")
    a = incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")], cause=("Deploy", dep))
    rep = run(g, a)
    n = g.read("MATCH (f:Fact) RETURN count(f)")[0][0]
    assert rep["lessons"] == n and 1 <= n <= cons.MAX_LESSONS
    b = incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")], cause=("Deploy", dep))
    run(g, b)
    assert g.read("MATCH (f:Fact) RETURN count(f)")[0][0] == n           # identical lessons again: nothing added
    fix =g.read("MATCH (f:Fact {predicate:'lesson:fix:bad_deploy'}) RETURN count(f)")[0][0]
    assert fix == 1                                                      # same lesson again: unchanged, not duplicated


def test_link_people_records_the_on_call_sre_and_the_author_of_the_cause(g):
    dep = deploy_of(g, "checkout-api")
    iid = incident(g, "checkout-api", [("rollback", {"service": "checkout-api"}, "success")], cause=("Deploy", dep))
    out = cons.link_people(g, iid, SIM_NOW)
    assert {p["role"] for p in out["linked"]} == {"responder", "author_of_cause"}
    sre = g.read("MATCH (p:Person)-[:WORKED_ON {role:'responder'}]->(:Incident {id:$i}) RETURN p.role", {"i": iid})
    assert sre == [["SRE"]]
    assert cons.link_people(g, iid, SIM_NOW + 1)["linked"] == out["linked"]       # idempotent
    assert g.read("MATCH (:Person)-[w:WORKED_ON]->(:Incident {id:$i}) RETURN count(w)", {"i": iid}) == [[2]]
    # who_knows (Q9) now ranks them for that service
    from api.graph import queries as q
    assert g.rows(q.Q9_WHO_KNOWS, {"svc": "checkout-api"})[0]["incidents"] == 1
