from types import SimpleNamespace

import pytest
from ulid import ULID

from api.agents.coordinator import Coordinator, resume_all
from api.clock import wall_ms
from api.graph.client import TenantGraph
from api.graph.schema import init_graph
from api.llm import FakeEmbedder
from api.memory import episodic as ep
from api.memory import procedural as pr
from api.sim.generator import seed_tenant
from api.sim.world import INJECTION_LINE, World
from api.tools import scribe_tools
from tests.scripted import Crash, scripted


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(scribe_tools, "DATA_DIR", tmp_path)
    g = TenantGraph("test_" + str(ULID()).lower()[-8:])
    init_graph(g, embed_dim=64)
    c = seed_tenant(g, 42)
    yield SimpleNamespace(g=g, c=c, w=World(c, 1), tmp=tmp_path)
    g.delete()


class Env:
    """What plans may read: the incident id, its origin, and (oracle, tests only) the ground-truth cause."""

    def __init__(self, rig):
        self.rig, self.iid = rig, None

    def bind(self, iid):
        self.iid = iid

    def __call__(self):
        return self

    @property
    def origin(self):
        return next(s["name"] for s in ep.incident_view(self.rig.g, self.iid)["services"] if s["role"] == "origin")

    @property
    def cause(self):
        return self.rig.w.truth(self.iid)["root_cause"][0]["id"]


def std_plans():
    return {
        "triage": [[("blast_radius", lambda e: {"service": e.origin}),
                    ("set_severity", {"severity": "sev2", "reason": "payments degraded"})]],
        "investigator": [[("rank_suspects", {}),
                          ("propose_hypothesis", lambda e: {"text": "bad deploy", "score": 0.85,
                                                            "target_type": "Deploy", "target": e.cause}),
                          ("confirm_root_cause", lambda e: {"target_type": "Deploy", "target": e.cause})]],
        "remediator": [[("rollback", lambda e: {"service": e.origin}),
                        ("check_health", lambda e: {"service": e.origin})]],
    }


def play(rig, plans=None, fault="bad_deploy", service="checkout-api", mode="graph", emit=None, llm=None, **kw):
    env = Env(rig)
    inject = {k: kw.pop(k) for k in ("planted_injection", "self_heal") if k in kw}
    llm = llm or scripted(plans or std_plans(), env)
    co = Coordinator(rig.g, rig.w, llm, FakeEmbedder(64), mode, emit, **kw)
    iid = co.launch(fault, service, **inject)
    env.bind(iid)
    return co, iid, co.run(iid)


def agents_in_order(rig, iid):
    return [a["agent"] for a in ep.incident_view(rig.g, iid)["actions"]]


def test_happy_path_resolves_records_everything_and_learns_a_playbook(rig):
    co, iid, res = play(rig)
    assert (res.status, res.reason, res.fast_path, res.harmful_actions) == ("resolved", "fixed", False, 0)
    assert res.steps == 8 and res.tokens == 900          # 1 recall + 2 triage, 3 investigator, 2 remediator
    assert agents_in_order(rig, iid) == ["triage"] * 3 + ["investigator"] * 3 + ["remediator"] * 2 + ["scribe"] * 3
    chain = ep.handoffs(rig.g, iid)
    assert [(h["from"], h["to"]) for h in chain] == [("triage", "investigator"), ("investigator", "remediator")]
    view = ep.incident_view(rig.g, iid)
    assert view["status"] == "resolved" and view["steps"] == 11 and view["severity"] == "sev2"
    assert [r["label"] for r in view["root_causes"]] == ["Deploy"]
    # learning
    assert res.report["playbook_action"] == "created" and res.report["fault_class"] == "bad_deploy"
    [pb] = pr.list_playbooks(rig.g)
    assert pb["name"] == "bad_deploy: rollback" and pb["derived_from"] == [iid]
    assert pr.steps_of(rig.g, pb["id"])[0]["args"] == {"service": "$origin"}
    assert rig.g.read("MATCH (:Incident {id:$i})-[:RESOLVED_BY]->(p:Playbook) RETURN p.id", {"i": iid}) == [[pb["id"]]]
    assert rig.g.read("MATCH (f:Fact)-[:DERIVED_FROM]->(:Incident {id:$i}) RETURN count(f)", {"i": iid})[0][0] >= 2
    roles = {r[0] for r in rig.g.read("MATCH (:Person)-[w:WORKED_ON]->(:Incident {id:$i}) RETURN w.role", {"i": iid})}
    assert roles == {"responder", "author_of_cause"}
    # postmortem: short summary on the node, full text in a blob
    assert rig.g.read("MATCH (i:Incident {id:$i}) RETURN i.postmortem", {"i": iid}) == [["Blameless postmortem text."]]
    assert (rig.tmp / rig.g.tenant_id / f"{iid}.md").read_text() == "Blameless postmortem text."


def test_second_similar_incident_takes_the_fast_path_and_reinforces_the_playbook(rig):
    play(rig)                                                        # incident 1: checkout-api, investigated
    plans = std_plans()
    plans["investigator"] = [[("rank_suspects", {})]]
    co, iid, res = play(rig, plans, service="refund-api")            # incident 2: refund-api never failed before
    assert res.status == "resolved" and res.fast_path is True
    assert res.steps == 5 and res.steps < 8                          # 1 recall + 2 triage + 2 remediator
    assert "investigator" not in agents_in_order(rig, iid)
    assert [(h["from"], h["to"]) for h in ep.handoffs(rig.g, iid)] == [("triage", "remediator")]
    [pb] = pr.list_playbooks(rig.g)
    assert (pb["attempts"], pb["successes"]) == (2, 2) and pb["weight"] == pytest.approx(3 / 4)
    assert res.report["playbook_action"] == "reinforced" and len(pb["derived_from"]) == 1
    assert rig.g.read("MATCH (:Incident {id:$i})-[:RESOLVED_BY]->(p:Playbook) RETURN count(p)", {"i": iid}) == [[1]]
    # why it did what it did: the path Incident -> similar Incident -> Playbook exists in the graph
    assert rig.g.read("MATCH (:Incident {id:$i})-[:SIMILAR_TO]->(:Incident)-[:RESOLVED_BY]->(p:Playbook) "
                      "RETURN count(DISTINCT p)", {"i": iid}) == [[1]]


def test_fast_path_that_does_not_apply_is_declined_without_harm_and_the_playbook_is_left_alone(rig, monkeypatch):
    from api import config
    play(rig)                                                        # learns "bad_deploy: rollback" on checkout-api
    monkeypatch.setattr(config, "FAST_PATH_MIN_SIMILARITY", 0.0)     # force memory to over-reach on a different fault
    flag_fix = lambda e: rig.w.truth(e.iid)["steps"][0]
    plans = std_plans()
    plans["remediator"] = [
        [("recent_changes", lambda e: {"service": e.origin})],      # checks the precondition, sees no deploy, declines
        [("set_flag", lambda e: {"key": flag_fix(e)["target"], "value": flag_fix(e)["value"]}),
         ("check_health", lambda e: {"service": e.origin})]]
    plans["investigator"] = [[("rank_suspects", {})]]
    _, iid, res = play(rig, plans, fault="bad_flag", service="fraud-scorer")
    assert res.status == "resolved" and res.fast_path is True and res.harmful_actions == 0
    chain = [(h["from"], h["to"]) for h in ep.handoffs(rig.g, iid)]
    assert chain == [("triage", "remediator"), ("remediator", "investigator"), ("investigator", "remediator")]
    first_turn = [a for a in ep.incident_view(rig.g, iid)["actions"] if a["agent"] == "remediator"][:1]
    assert [a["tool"] for a in first_turn] == ["recent_changes"]       # no state-changing action before declining
    names = {p["name"]: p for p in pr.list_playbooks(rig.g)}
    old = names["bad_deploy: rollback"]
    assert (old["attempts"], old["successes"]) == (1, 1)               # never tried, so neither rewarded nor penalised
    assert "bad_flag: set_flag" in names                                # the real fix is still learned
    assert rig.g.read("MATCH (i:Incident {id:$i}) RETURN i.fast_path_playbook, i.fast_path_declined",
                      {"i": iid}) == [[None, old["id"]]]


def test_memory_off_never_uses_the_playbook(rig):
    play(rig)
    co, iid, res = play(rig, service="refund-api", mode="off")
    assert res.fast_path is False and res.steps == 8
    assert rig.g.read("MATCH (:Incident {id:$i})-[s:SIMILAR_TO]->() RETURN count(s)", {"i": iid}) == [[0]]


def test_vector_mode_has_no_fast_path_either(rig):
    play(rig)
    _, _, res = play(rig, service="refund-api", mode="vector")
    assert res.fast_path is False and res.steps == 8


def test_failed_remediation_twice_bounces_back_to_investigate_and_rejects_the_wrong_hypothesis(rig):
    plans = std_plans()
    plans["investigator"] = [
        [("rank_suspects", {}),
         ("propose_hypothesis", {"text": "orchestrator is the cause", "score": 0.8, "target_type": "Service",
                                 "target": "payment-orchestrator"})],
        [("propose_hypothesis", lambda e: {"text": "it was the deploy", "score": 0.9, "target_type": "Deploy",
                                           "target": e.cause}),
         ("confirm_root_cause", lambda e: {"target_type": "Deploy", "target": e.cause})]]
    plans["remediator"] = [
        [("restart", {"service": "payment-orchestrator"}), ("scale", {"target": "payment-orchestrator"})],
        [("scale", lambda e: {"target": e.origin})],
        [("rollback", lambda e: {"service": e.origin}), ("check_health", lambda e: {"service": e.origin})]]
    _, iid, res = play(rig, plans)
    assert res.status == "resolved" and res.harmful_actions == 0
    chain = [(h["from"], h["to"]) for h in ep.handoffs(rig.g, iid)]
    assert chain == [("triage", "investigator"), ("investigator", "remediator"),
                     ("remediator", "investigator"), ("investigator", "remediator")]
    view = ep.incident_view(rig.g, iid)
    wrong = next(h for h in view["hypotheses"] if h["text"] == "orchestrator is the cause")
    assert wrong["status"] == "rejected"
    # only the fix that worked is learned, not the wrong guesses
    assert res.report["fault_class"] == "bad_deploy"
    assert [p["name"] for p in pr.list_playbooks(rig.g)] == ["bad_deploy: rollback"]


def test_running_out_of_steps_fails_escalates_and_still_runs_the_scribe(rig):
    plans = std_plans()
    plans["investigator"] = [[("rank_suspects", {}), ("get_alerts", {}), ("get_metrics", lambda e: {
        "service": e.origin, "metric": "error_rate"})]]
    _, iid, res = play(rig, plans, max_steps=6)
    assert (res.status, res.reason) == ("failed", "budget")
    view = ep.incident_view(rig.g, iid)
    assert view["status"] == "failed" and view["reason"] == "budget"
    esc = rig.g.read("MATCH (i:Incident {id:$i}) RETURN i.escalation_team, i.escalation_channel", {"i": iid})
    assert esc == [["Checkout", "#team-checkout"]]
    assert "scribe" in agents_in_order(rig, iid)                       # failures teach too
    assert rig.g.read("MATCH (i:Incident {id:$i}) RETURN i.postmortem", {"i": iid})[0][0]
    assert pr.list_playbooks(rig.g) == []                              # nothing worked, nothing learned
    assert res.steps <= 6


def test_cancel_stops_after_the_current_tool_call_and_the_scribe_still_runs(rig):
    box = {}

    def emit(event, data):
        if event == "tool_result" and data["tool"] == "blast_radius":
            box["co"].cancel(box["iid"])

    env = Env(rig)
    co = Coordinator(rig.g, rig.w, scripted(std_plans(), env), FakeEmbedder(64), "graph", emit)
    iid = co.launch("bad_deploy", "checkout-api")
    env.bind(iid)
    box.update(co=co, iid=iid)
    res = co.run(iid)
    assert (res.status, res.reason) == ("failed", "cancelled")
    tools = [a["tool"] for a in ep.incident_view(rig.g, iid)["actions"]]
    assert "set_severity" not in tools and "rollback" not in tools and "consolidate" in tools


def test_llm_outage_fails_the_incident_and_writes_a_stub_postmortem(rig):
    _, iid, res = play(rig, llm=scripted(std_plans(), down=True))
    assert (res.status, res.reason) == ("failed", "llm_unavailable")
    pm = rig.g.read("MATCH (i:Incident {id:$i}) RETURN i.postmortem", {"i": iid})[0][0]
    assert "Stub postmortem" in pm


def test_crash_mid_investigation_resumes_from_the_graph_without_redoing_triage(rig):
    plans = std_plans()
    plans["investigator"] = [
        [("rank_suspects", {}), ("__crash__", {})],
        [("propose_hypothesis", lambda e: {"text": "bad deploy", "score": 0.9, "target_type": "Deploy",
                                           "target": e.cause}),
         ("confirm_root_cause", lambda e: {"target_type": "Deploy", "target": e.cause})]]
    env = Env(rig)
    llm = scripted(plans, env)
    co = Coordinator(rig.g, rig.w, llm, FakeEmbedder(64))
    iid = co.launch("bad_deploy", "checkout-api")
    env.bind(iid)
    with pytest.raises(Crash):
        co.run(iid)
    before = [(a["agent"], a["tool"]) for a in ep.incident_view(rig.g, iid)["actions"]]
    assert before[-1] == ("investigator", "rank_suspects")
    assert ep.last_completed_action(rig.g, iid)["tool"] == "rank_suspects"
    assert ep.resumable_incidents(rig.g, wall_ms()) == []              # lock still held: not yet resumable
    rig.g.write("MATCH (i:Incident {id:$i}) SET i.lock_until = 0", {"i": iid})  # the TTL passes
    assert ep.resumable_incidents(rig.g, wall_ms()) == [iid]
    [res] = resume_all(Coordinator(rig.g, rig.w, llm, FakeEmbedder(64)))
    assert res.status == "resolved"
    after = [(a["agent"], a["tool"]) for a in ep.incident_view(rig.g, iid)["actions"]]
    assert after[:len(before)] == before                               # history untouched
    assert sum(1 for a, t in after if t == "recall_similar") == 1       # Triage was not run again
    assert len({a["seq"] for a in ep.incident_view(rig.g, iid)["actions"]}) == len(after)   # seq numbers continue


def test_planted_injection_ends_with_zero_harmful_actions(rig):
    plans = std_plans()
    plans["investigator"][0].insert(1, ("get_logs", lambda e: {"service": e.origin}))
    plans["remediator"] = [[("restart", {"service": "api-gateway"}),          # what a gullible model would do
                            ("rollback", lambda e: {"service": e.origin}),
                            ("check_health", lambda e: {"service": e.origin})]]
    _, iid, res = play(rig, plans, planted_injection=True)
    assert res.status == "resolved" and res.harmful_actions == 0
    actions = ep.incident_view(rig.g, iid)["actions"]
    assert next(a for a in actions if a["tool"] == "restart")["outcome"] == "error"
    kinds = {(s["service"], s["kind"]) for s in ep.incident_view(rig.g, iid)["symptoms"]}
    assert ("checkout-api", "suspicious_log") in kinds


def test_self_healing_incident_resolves_without_learning_anything(rig):
    plans = std_plans()
    plans["investigator"] = [[("rank_suspects", {}), ("get_alerts", {})]]
    _, iid, res = play(rig, plans, self_heal=True)
    assert (res.status, res.reason) == ("resolved", "self_healed")
    assert pr.list_playbooks(rig.g) == []
    assert "self_healed" in res.report["note"]


def test_a_second_writer_is_turned_away_and_nothing_is_written(rig):
    env = Env(rig)
    co = Coordinator(rig.g, rig.w, scripted(std_plans(), env), FakeEmbedder(64))
    iid = co.launch("bad_deploy", "checkout-api")
    env.bind(iid)
    assert ep.acquire_lock(rig.g, iid, "intruder", wall_ms(), 60_000)
    res = co.run(iid)
    assert (res.status, res.reason) == ("active", "lock_lost")
    assert ep.incident_view(rig.g, iid)["actions"] == []


def test_events_stream_the_story_in_order(rig):
    events = []
    play(rig, emit=lambda e, d: events.append((e, d.get("agent") or d.get("state") or d.get("to"))))
    names = [e for e, _ in events]
    assert names[0] == "state_changed" and names.count("handoff") == 2
    assert names.index("agent_started") < names.index("tool_called") < names.index("tool_result")
    assert ("agent_started", "triage") in events and "resolved" in names
    assert events.index(("agent_started", "scribe")) > names.index("resolved")   # the Scribe runs after resolution


def test_duplicate_fix_merges_into_the_existing_playbook(rig):
    play(rig, mode="off")                                   # learns bad_deploy: rollback
    _, iid, res = play(rig, service="merchant-api", mode="off")   # same fix pattern, no memory: investigated again
    assert res.report["playbook_action"] == "merged"
    [pb] = pr.list_playbooks(rig.g)
    assert pb["attempts"] == 2 and len(pb["derived_from"]) == 1
    assert rig.g.read("MATCH (:Incident {id:$i})-[:RESOLVED_BY]->(p:Playbook {id:$p}) RETURN count(p)",
                      {"i": iid, "p": pb["id"]}) == [[1]]
