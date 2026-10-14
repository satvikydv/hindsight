import pytest

from api.graph import queries as q


def test_blast_radius_of_5_node_dag(dag):
    rows = dag.rows(q.Q1_BLAST_RADIUS, {"svc": "ledger"})
    hops = {r["service"]: r["hops"] for r in rows}
    assert hops == {"payments": 1, "checkout": 2, "refund": 2, "gw": 3}


def test_blast_radius_ignores_closed_edges(dag):
    dag.write(
        "MATCH (:Service {name:'refund'})-[r:DEPENDS_ON]->(:Service {name:'payments'}) SET r.valid_to = 5"
    )
    hops = {r["service"]: r["hops"] for r in dag.rows(q.Q1_BLAST_RADIUS, {"svc": "ledger"})}
    assert "refund" not in hops
    assert hops["gw"] == 3  # still reachable through checkout


def test_blast_radius_of_leaf_caller_is_empty(dag):
    assert dag.rows(q.Q1_BLAST_RADIUS, {"svc": "gw"}) == []


def test_dependency_path_length(dag):
    rows = dag.read(
        "MATCH (a:Service {name:$f}), (b:Service {name:$t}) "
        "RETURN length(shortestPath((a)-[:DEPENDS_ON*]->(b)))",
        {"f": "gw", "t": "ledger"},
    )
    assert rows == [[3]]
    # Q10 itself must run and return a path value
    assert len(dag.read(q.Q10_DEPENDENCY_PATH, {"from": "gw", "to": "ledger"})) == 1


def _seed_ownership(g):
    g.write("""
        CREATE (a:Team {name:'Payments'}), (b:Team {name:'Ledger'}),
               (s:Service {name:'payments-db'}),
               (a)-[:OWNS {valid_from: 1, source:'seed'}]->(s),
               (p:Person {name:'Ana'}), (p)-[:MEMBER_OF {valid_from: 1}]->(a)
    """)


def test_owners_current(graph):
    _seed_ownership(graph)
    rows = graph.rows(q.Q8_OWNERS, {"svc": "payments-db"})
    assert rows == [{"team": "Payments", "channel": None, "members": ["Ana"]}]


def test_supersede_ownership_keeps_history(graph):
    _seed_ownership(graph)
    graph.write(q.Q14_SUPERSEDE_OWNERSHIP, {"svc": "payments-db", "team": "Ledger", "now": 100, "source": "human"})

    current = graph.rows(q.Q8_OWNERS, {"svc": "payments-db"})
    assert [r["team"] for r in current] == ["Ledger"]

    history = graph.read(
        "MATCH (t:Team)-[o:OWNS]->(:Service {name:'payments-db'}) "
        "RETURN t.name, o.valid_from, o.valid_to ORDER BY o.valid_from"
    )
    assert history == [["Payments", 1, 100], ["Ledger", 100, None]]


def test_reinforce_playbook_laplace_weight(graph):
    graph.write("CREATE (:Playbook {id:'pb1', attempt_count:0, success_count:0, weight:0.5})")
    w1 = graph.write(q.Q13_REINFORCE_PLAYBOOK, {"pid": "pb1", "won": 1, "now": 1})[0][0]
    assert w1 == pytest.approx((1 + 1) / (1 + 2))
    w2 = graph.write(q.Q13_REINFORCE_PLAYBOOK, {"pid": "pb1", "won": 0, "now": 2})[0][0]
    assert w2 == pytest.approx((1 + 1) / (2 + 2))


def test_incident_lock_blocks_second_writer_until_expiry(graph):
    graph.write("CREATE (:Incident {id:'i1'})")
    p = {"iid": "i1", "ttl_ms": 30_000}
    assert graph.write(q.Q15_ACQUIRE_LOCK, {**p, "agent": "triage", "now": 1_000}) == [["triage"]]
    assert graph.write(q.Q15_ACQUIRE_LOCK, {**p, "agent": "investigator", "now": 2_000}) == []
    assert graph.write(q.Q15_ACQUIRE_LOCK, {**p, "agent": "triage", "now": 3_000}) == [["triage"]]  # holder refreshes
    assert graph.write(q.Q15_ACQUIRE_LOCK, {**p, "agent": "investigator", "now": 20_000}) == []  # refresh extended it
    assert graph.write(q.Q15_ACQUIRE_LOCK, {**p, "agent": "investigator", "now": 40_000}) == [["investigator"]]


def test_handoff_chain_ordered(graph):
    graph.write("""
        CREATE (t:Agent {name:'triage'}), (i:Agent {name:'investigator'}), (r:Agent {name:'remediator'}),
               (t)-[:HANDOFF {incident_id:'i1', seq:1, at:10, summary:'blast radius done'}]->(i),
               (i)-[:HANDOFF {incident_id:'i1', seq:2, at:20, summary:'deploy suspected'}]->(r),
               (t)-[:HANDOFF {incident_id:'other', seq:1, at:5, summary:'noise'}]->(i)
    """)
    rows = graph.rows(q.Q16_HANDOFF_CHAIN, {"iid": "i1"})
    assert [(r["from"], r["to"]) for r in rows] == [("triage", "investigator"), ("investigator", "remediator")]
