import pytest

from api.memory import semantic as sem
from api.sim.generator import SIM_NOW, seed_tenant


@pytest.fixture
def g(graph):
    seed_tenant(graph, 42)
    return graph


def svc(g, name="refund-api"):
    return "Service", g.read("MATCH (s:Service {name:$n}) RETURN s.id", {"n": name})[0][0]


def test_first_fact_is_created_and_visible_now(g):
    label, sid = svc(g)
    r = sem.write_fact(g, label, sid, "retries", "3", "refund-api retries 3x", "agent", at=100)
    assert r["status"] == "created"
    assert [f["fact"] for f in sem.facts_about(g, sid, as_of=200)] == ["refund-api retries 3x"]


def test_same_value_is_unchanged_different_value_supersedes_and_keeps_history(g):
    label, sid = svc(g)
    sem.write_fact(g, label, sid, "retries", "3", "refund-api retries 3x", "agent", at=100)
    assert sem.write_fact(g, label, sid, "retries", "3", "again", "agent", at=150)["status"] == "unchanged"
    r = sem.write_fact(g, label, sid, "retries", "5", "refund-api retries 5x", "agent", at=300, reason="config changed")
    assert r["status"] == "superseded"
    assert [f["fact"] for f in sem.facts_about(g, sid, as_of=400)] == ["refund-api retries 5x"]
    assert [f["fact"] for f in sem.facts_about(g, sid, as_of=200)] == ["refund-api retries 3x"]  # what we believed then
    assert sem.facts_about(g, sid, as_of=50) == []
    assert [(h["value"], h["valid_from"], h["valid_to"]) for h in sem.fact_history(g, sid, "retries")] == [
        ("3", 100, 300), ("5", 300, None)]
    assert g.read("MATCH (n:Fact)-[s:SUPERSEDES]->(o:Fact) RETURN n.value, o.value, s.reason") == [
        ["5", "3", "config changed"]]


def test_human_fact_beats_agent_fact_but_not_the_other_way_round(g):
    label, sid = svc(g)
    sem.write_fact(g, label, sid, "retries", "3", "agent says 3", "agent", at=100)
    assert sem.write_fact(g, label, sid, "retries", "4", "human says 4", "human", at=200)["status"] == "superseded"
    assert sem.write_fact(g, label, sid, "retries", "9", "agent says 9", "agent", at=300)["status"] == "kept_human"
    assert [f["fact"] for f in sem.facts_about(g, sid, as_of=400)] == ["human says 4"]
    assert sem.write_fact(g, label, sid, "retries", "6", "human again", "human", at=500)["status"] == "superseded"


def test_distinct_predicates_do_not_supersede_each_other(g):
    label, sid = svc(g)
    sem.write_fact(g, label, sid, "lesson:no_circuit_breaker", "ledger-db", "no breaker on ledger-db", "agent", 100)
    sem.write_fact(g, label, sid, "lesson:retry_storm", "checkout", "retry storm with checkout", "agent", 100)
    assert len(sem.facts_about(g, sid, as_of=200)) == 2


def test_fact_links_to_its_incident_and_stores_embedding(g):
    label, sid = svc(g)
    g.write("CREATE (:Incident {id:'inc1'})")
    sem.write_fact(g, label, sid, "lesson:x", "y", "lesson", "agent", 100, derived_from="inc1",
                   embedding=[1.0, 0.0, 0.0, 0.0])
    assert g.read("MATCH (f:Fact)-[:DERIVED_FROM]->(i:Incident) RETURN i.id") == [["inc1"]]
    assert g.read("CALL db.idx.vector.queryNodes('Fact','embedding',1,vecf32([1.0,0.0,0.0,0.0])) "
                  "YIELD node RETURN node.predicate") == [["lesson:x"]]  # fixture graph has a dim-4 index


def test_cannot_write_a_fact_about_an_unlisted_label(g):
    with pytest.raises(ValueError):
        sem.write_fact(g, "Incident) DETACH DELETE (n", "x", "p", "v", "t", "agent", 1)


def test_ownership_correction_supersedes_edge_and_old_answer_stays_available(g):
    assert sem.current_owner(g, "payments-db") is None            # datastores are not owned in the seed
    assert sem.current_owner(g, "checkout-api") == "Checkout"
    out = sem.correct(g, "checkout-api", "owner", "Ledger", "reorg", at=SIM_NOW + 5_000)
    assert out == {"status": "superseded", "owner": "Ledger", "previous": "Checkout"}
    assert sem.current_owner(g, "checkout-api") == "Ledger"
    assert sem.owners_as_of(g, "checkout-api", SIM_NOW + 4_000) == ["Checkout"]          # belief at the time of incident #1
    assert sem.owners_as_of(g, "checkout-api", SIM_NOW + 6_000) == ["Ledger"]
    _, sid = svc(g, "checkout-api")
    assert [f["fact"] for f in sem.facts_about(g, sid, as_of=SIM_NOW + 6_000)] == [
        "checkout-api is now owned by Team Ledger (reorg)"]
    assert sem.correct(g, "checkout-api", "owner", "Ledger", "again", at=SIM_NOW + 7_000)["status"] == "unchanged"


def test_ownership_of_an_unowned_service_is_created_not_dropped(g):
    g.write("MATCH (:Team)-[o:OWNS]->(:Service {name:'tokenizer'}) SET o.valid_to = " + str(SIM_NOW))
    assert sem.current_owner(g, "tokenizer") is None
    out = sem.correct(g, "tokenizer", "owner", "Risk", "adopted", at=SIM_NOW + 20)
    assert out["status"] == "created" and sem.current_owner(g, "tokenizer") == "Risk"


def test_unknown_subject_or_team_raises_with_suggestions(g):
    with pytest.raises(sem.NotFound) as e:
        sem.correct(g, "checkout-apy", "owner", "Ledger", "typo", at=1)
    assert "checkout-api" in e.value.suggestions
    with pytest.raises(sem.NotFound) as e:
        sem.correct(g, "checkout-api", "owner", "Ledgr", "typo", at=1)
    assert "Ledger" in e.value.suggestions
    assert sem.current_owner(g, "checkout-api") == "Checkout"          # nothing changed


def test_generic_correction_becomes_a_human_fact(g):
    out = sem.correct(g, "refund-api", "retries", "5", "ops confirmed", at=100)
    assert out["status"] == "created"
    _, sid = svc(g)
    assert sem.write_fact(g, "Service", sid, "retries", "7", "agent guess", "agent", 200)["status"] == "kept_human"
