import pytest

from api.ids import new_id
from api.memory import episodic as ep
from api.memory import procedural as pr
from api.sim.generator import seed_tenant

ROLLBACK = [{"tool": "rollback", "args_template": {"service": "$origin"}}]
DB_FIX = [{"tool": "scale", "args_template": {"target": "$datastore"}},
          {"tool": "restart", "args_template": {"service": "$top_caller"}}]


@pytest.fixture
def g(graph):
    seed_tenant(graph, 42)
    return graph


def resolved_incident(g, at=1000):
    iid = new_id()
    ep.open_incident(g, iid, "t", "sev2", at, "checkout-api")
    ep.set_status(g, iid, "resolved", at + 100)
    return iid


def test_created_playbook_has_ordered_linked_steps_and_provenance(g):
    iid = resolved_incident(g)
    pid = pr.create_playbook(g, "db pool", "db_pool_exhaustion", DB_FIX, iid, at=2000)
    steps = pr.steps_of(g, pid)
    assert [(s["step"], s["tool"], s["args"]) for s in steps] == [
        (1, "scale", {"target": "$datastore"}), (2, "restart", {"service": "$top_caller"})]
    assert all(s["wins"] == 1 and s["tries"] == 1 for s in steps)
    assert g.read("MATCH (:Step {order:1})-[:NEXT]->(b:Step) RETURN b.order") == [[2]]
    assert g.read("MATCH (:Incident {id:$i})-[:RESOLVED_BY]->(p:Playbook {id:$p}) RETURN count(p)",
                  {"i": iid, "p": pid}) == [[1]]
    [pb] = pr.list_playbooks(g)
    assert pb["derived_from"] == [iid] and pb["status"] == "active"
    assert pb["weight"] == pytest.approx(2 / 3) and (pb["successes"], pb["attempts"]) == (1, 1)


def test_find_duplicate_matches_same_class_and_identical_sequence_only(g):
    iid = resolved_incident(g)
    pid = pr.create_playbook(g, "rollback", "bad_deploy", ROLLBACK, iid, 2000)
    assert pr.find_duplicate(g, "bad_deploy", ROLLBACK) == pid
    assert pr.find_duplicate(g, "memory_leak", ROLLBACK) is None                       # other class
    assert pr.find_duplicate(g, "bad_deploy", [{"tool": "restart", "args_template": {"service": "$origin"}}]) is None
    assert pr.find_duplicate(g, "bad_deploy", ROLLBACK + ROLLBACK) is None            # different length


def test_reinforce_updates_counts_and_laplace_weight(g):
    pid = pr.create_playbook(g, "rollback", "bad_deploy", ROLLBACK, resolved_incident(g), 2000)
    out = pr.reinforce(g, pid, won=True, at=3000)
    assert out == {"weight": pytest.approx(3 / 4), "status": "active"}      # (2+1)/(2+2)
    out = pr.reinforce(g, pid, won=False, at=4000)
    assert out["weight"] == pytest.approx(3 / 5)                              # (2+1)/(3+2)
    assert g.read("MATCH (p:Playbook {id:$p}) RETURN p.last_used_at", {"p": pid}) == [[4000]]


def test_playbook_with_3_attempts_under_30_percent_success_is_deprecated(g):
    pid = pr.create_playbook(g, "lucky fix", "bad_deploy", ROLLBACK, resolved_incident(g), 2000)  # 1/1
    assert pr.reinforce(g, pid, False, 3000)["status"] == "active"           # 1/2
    assert pr.reinforce(g, pid, False, 4000)["status"] == "active"           # 1/3 = 33%
    assert pr.reinforce(g, pid, False, 5000)["status"] == "deprecated"       # 1/4 = 25%
    assert pr.find_duplicate(g, "bad_deploy", ROLLBACK) is None               # kept, but never merged into or recalled


def test_deprecated_playbooks_are_never_candidates(g):
    i_ok, i_bad = resolved_incident(g), resolved_incident(g)
    ok = pr.create_playbook(g, "good", "bad_deploy", ROLLBACK, i_ok, 2000)
    bad = pr.create_playbook(g, "bad", "bad_deploy", [{"tool": "restart", "args_template": {}}], i_bad, 2000)
    for _ in range(3):
        pr.reinforce(g, bad, False, 3000)
    got = pr.candidates(g, [i_ok, i_bad])
    assert [c["playbook"] for c in got] == [ok]
    assert got[0]["support"] == 1 and got[0]["fault_class"] == "bad_deploy"


def test_candidates_order_by_weight_then_support(g):
    i1, i2, i3 = resolved_incident(g), resolved_incident(g), resolved_incident(g)
    strong = pr.create_playbook(g, "strong", "bad_deploy", ROLLBACK, i1, 2000)
    pr.reinforce(g, strong, True, 2500)
    weak = pr.create_playbook(g, "weak", "bad_deploy", [{"tool": "restart", "args_template": {}}], i2, 2000)
    g.write("MATCH (i:Incident {id:$i}), (p:Playbook {id:$p}) CREATE (i)-[:RESOLVED_BY]->(p)", {"i": i3, "p": strong})
    got = pr.candidates(g, [i1, i2, i3])
    assert [c["playbook"] for c in got] == [strong, weak]
    assert got[0]["support"] == 2 and got[0]["weight"] > got[1]["weight"]


def test_step_counts(g):
    pid = pr.create_playbook(g, "db pool", "db_pool_exhaustion", DB_FIX, resolved_incident(g), 2000)
    pr.reinforce_steps(g, pid, {1: True, 2: False})
    steps = {s["step"]: (s["wins"], s["tries"]) for s in pr.steps_of(g, pid)}
    assert steps == {1: (2, 2), 2: (1, 2)}


def test_weight_halves_every_30_days_unused_and_decay_does_not_compound(g):
    pid = pr.create_playbook(g, "rollback", "bad_deploy", ROLLBACK, resolved_incident(g), at=0)
    weight = lambda: g.read("MATCH (p:Playbook {id:$p}) RETURN p.weight", {"p": pid})[0][0]
    base = 2 / 3
    pr.apply_decay(g, now=30 * pr.DAY_MS)
    assert weight() == pytest.approx(base / 2)
    pr.apply_decay(g, now=30 * pr.DAY_MS)                       # same instant again: unchanged
    assert weight() == pytest.approx(base / 2)
    pr.apply_decay(g, now=60 * pr.DAY_MS)
    assert weight() == pytest.approx(base / 4)
    pr.reinforce(g, pid, True, at=60 * pr.DAY_MS)               # use resets the clock and refreshes the base
    assert weight() == pytest.approx(3 / 4)
