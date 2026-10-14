import json

import pytest

from api.graph import queries as q
from api.ids import new_id
from api.memory import episodic as ep
from api.sim.generator import seed_tenant


@pytest.fixture
def g(graph):
    seed_tenant(graph, 42)
    return graph


@pytest.fixture
def inc(g):
    iid = new_id()
    ep.open_incident(g, iid, "checkout-api errors", "sev2", at=1000, origin="checkout-api",
                     impacted=["checkout-api", "api-gateway"])
    return iid


def count(g, cypher, **p):
    return g.read(cypher, p)[0][0]


def service_id(g, name):
    return g.read("MATCH (s:Service {name:$n}) RETURN s.id", {"n": name})[0][0]


def test_open_incident_links_origin_and_impacted_once(g, inc):
    ep.open_incident(g, inc, "again", "sev1", at=2000, origin="checkout-api")  # retry must not duplicate
    assert count(g, "MATCH (i:Incident {id:$i}) RETURN count(i)", i=inc) == 1
    roles = g.rows("MATCH (:Incident {id:$i})-[r:AFFECTS]->(s) RETURN s.name AS s, r.role AS role ORDER BY s", {"i": inc})
    assert roles == [{"s": "api-gateway", "role": "impacted"}, {"s": "checkout-api", "role": "origin"}]
    assert g.read("MATCH (i:Incident {id:$i}) RETURN i.status, i.title", {"i": inc}) == [["open", "checkout-api errors"]]


def test_action_is_written_pending_before_the_tool_runs_then_finished(g, inc):
    aid = new_id()
    ep.begin_action(g, inc, aid, "triage", "blast_radius", {"service": "checkout-api", "max_hops": 4}, 1, 1100, "scope it")
    mid = ep.incident_view(g, inc)["actions"]
    assert [(a["tool"], a["outcome"], a["agent"]) for a in mid] == [("blast_radius", "pending", "triage")]
    assert json.loads(mid[0]["args"]) == {"max_hops": 4, "service": "checkout-api"}
    ep.finish_action(g, aid, "success", "1 service in radius", 1200)
    done = ep.incident_view(g, inc)["actions"][0]
    assert done["outcome"] == "success" and done["summary"] == "1 service in radius"


def test_begin_action_is_idempotent(g, inc):
    aid = new_id()
    for _ in range(3):
        ep.begin_action(g, inc, aid, "triage", "get_alerts", {}, 1, 1100)
    assert count(g, "MATCH (a:Action {id:$a}) RETURN count(a)", a=aid) == 1
    assert count(g, "MATCH (:Incident {id:$i})-[t:TOOK]->(:Action {id:$a}) RETURN count(t)", i=inc, a=aid) == 1
    assert count(g, "MATCH (:Action {id:$a})-[b:BY]->(:Agent) RETURN count(b)", a=aid) == 1


def test_cited_nodes_come_back_through_explain_action(g, inc):
    aid = new_id()
    ep.begin_action(g, inc, aid, "investigator", "rank_suspects", {}, 2, 1100)
    ep.cite(g, aid, "Service", service_id(g, "checkout-api"), "origin of the incident")
    ep.cite(g, aid, "Incident", inc, "this incident")
    ep.cite(g, aid, "Service", service_id(g, "checkout-api"), "origin of the incident")  # retry
    row = g.rows(q.Q11_EXPLAIN_ACTION, {"aid": aid})[0]
    assert row["agent"] == "investigator"
    assert sorted(e["why"] for e in row["evidence"]) == ["origin of the incident", "this incident"]
    with pytest.raises(ValueError):
        ep.cite(g, aid, "Service) DETACH DELETE (n", "x", "injection attempt")


def test_resume_point_is_the_last_completed_action(g, inc):
    a1, a2 = new_id(), new_id()
    ep.begin_action(g, inc, a1, "triage", "get_alerts", {}, 1, 1100)
    ep.finish_action(g, a1, "success", "ok", 1150)
    ep.begin_action(g, inc, a2, "triage", "blast_radius", {}, 2, 1200)  # server dies before finish_action
    last = ep.last_completed_action(g, inc)
    assert last["id"] == a1 and last["seq"] == 1
    assert ep.last_completed_action(g, new_id()) is None


def test_handoff_chain_is_ordered_and_idempotent(g, inc):
    ep.write_handoff(g, inc, "investigator", "remediator", 2, "deploy v1.6.0 suspected", 1300)
    ep.write_handoff(g, inc, "triage", "investigator", 1, "blast radius is 1 service", 1200)
    ep.write_handoff(g, inc, "triage", "investigator", 1, "blast radius is 1 service", 1200)
    chain = ep.handoffs(g, inc)
    assert [(h["from"], h["to"]) for h in chain] == [("triage", "investigator"), ("investigator", "remediator")]
    assert ep.incident_view(g, inc)["handoffs"] == chain


def test_lock_blocks_others_refreshes_for_holder_and_releases(g, inc):
    assert ep.acquire_lock(g, inc, "triage", now=1000)
    assert not ep.acquire_lock(g, inc, "investigator", now=2000)
    assert ep.acquire_lock(g, inc, "triage", now=20_000)           # holder refreshes the TTL
    assert not ep.acquire_lock(g, inc, "investigator", now=40_000)
    ep.release_lock(g, inc, "investigator")                          # not the holder: no effect
    assert not ep.acquire_lock(g, inc, "investigator", now=41_000)
    ep.release_lock(g, inc, "triage")
    assert ep.acquire_lock(g, inc, "investigator", now=41_000)


def test_resumable_incidents_are_active_with_free_or_expired_lock(g):
    a, b, c, d = (new_id() for _ in range(4))
    for i, at in ((a, 1), (b, 2), (c, 3), (d, 4)):
        ep.open_incident(g, i, "t", "sev3", at, "checkout-api")
    ep.acquire_lock(g, b, "triage", now=1000, ttl_ms=30_000)       # held and live
    ep.acquire_lock(g, c, "triage", now=1000, ttl_ms=100)          # held but expired by now=5000
    ep.set_status(g, d, "resolved", 4000)                           # finished
    assert ep.resumable_incidents(g, now=5000) == [a, c]


def test_messages_reload_last_n_oldest_first(g, inc):
    for seq in range(1, 6):
        ep.add_message(g, inc, new_id(), "triage", "assistant", f"turn {seq}", seq, 1000 + seq)
    ep.add_message(g, new_id(), new_id(), "triage", "assistant", "other incident", 1, 1)  # unknown incident: no link
    msgs = ep.load_messages(g, inc, n=3)
    assert [m["content"] for m in msgs] == ["turn 3", "turn 4", "turn 5"]
    ep.add_message(g, inc, new_id(), "scribe", "assistant", "x" * 5000, 6, 2000)
    assert len(ep.load_messages(g, inc, n=1)[0]["content"]) == ep.MAX_MESSAGE_CHARS


def test_hypotheses_and_root_cause_appear_in_the_incident_view(g, inc):
    dep = g.read("MATCH (d:Deploy)-[:DEPLOYED]->(:Service {name:'checkout-api'}) RETURN d.id LIMIT 1")[0][0]
    h1, h2 = new_id(), new_id()
    ep.add_hypothesis(g, inc, h1, "investigator", "bad deploy on checkout-api", 0.4, 1100, "Deploy", dep)
    ep.add_hypothesis(g, inc, h2, "investigator", "config flag", 0.2, 1100, "Service", service_id(g, "checkout-api"))
    ep.add_hypothesis(g, inc, h1, "investigator", "bad deploy on checkout-api", 0.9, 1200, "Deploy", dep)  # rescore
    ep.update_hypothesis(g, h2, "rejected")
    ep.confirm_root_cause(g, inc, "Deploy", dep, "investigator")
    view = ep.incident_view(g, inc)
    assert [(h["id"], h["score"], h["status"]) for h in view["hypotheses"]] == [(h1, 0.9, "open"), (h2, 0.2, "rejected")]
    assert view["hypotheses"][0]["target_label"] == "Deploy"
    assert view["root_causes"] == [{"label": "Deploy", "id": dep, "confirmed_by": "investigator"}]


def test_symptoms_usage_and_status(g, inc):
    syms = [{"service": "checkout-api", "kind": "error_rate", "metric": "error_rate", "value": 0.58},
            {"service": "payments-db", "kind": "saturation", "metric": "saturation", "value": 0.97}]
    ep.add_symptoms(g, inc, syms)
    ep.add_symptoms(g, inc, syms)  # idempotent, including a datastore target
    view = ep.incident_view(g, inc)
    assert [(s["service"], s["kind"]) for s in view["symptoms"]] == [("checkout-api", "error_rate"),
                                                                     ("payments-db", "saturation")]
    ep.add_usage(g, inc, steps=2, tokens=900)
    ep.add_usage(g, inc, steps=1, tokens=100)
    ep.set_status(g, inc, "resolved", 5000)
    view = ep.incident_view(g, inc)
    assert (view["steps"], view["tokens"], view["status"], view["resolved_at"]) == (3, 1000, "resolved", 5000)
    assert ep.incident_view(g, new_id()) is None
