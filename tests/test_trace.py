"""Traceability: per-call LLM telemetry, LED_TO links, explain_action, incident_trace, markdown export.

Scripted calls report 100 tokens: 90 input + 10 output. Priced from the real table that is
strong (gpt-4.1)  : 90 * 2.00 + 10 * 8.00 = 260 / 1e6 = $0.000260 per call
fast   (gpt-4.1-mini): 90 * 0.40 + 10 * 1.60 =  52 / 1e6 = $0.000052 per call
"""

import pytest

from api.memory import episodic as ep
from api.memory import trace
from tests.test_coordinator import Env, play, rig, std_plans  # noqa: F401  (rig is a fixture)

STRONG, FAST = 0.000260, 0.000052


def msgs(rig, iid):
    return rig.g.rows("MATCH (m:Message {role:'assistant'})-[l:IN_LOG]->(:Incident {id:$i}) "
                      "RETURN m.agent AS agent, m.model AS model, m.prompt_tokens AS p, m.completion_tokens AS c, "
                      "m.cost_usd AS cost ORDER BY l.seq", {"i": iid})


def test_every_llm_turn_records_model_tokens_and_cost_on_its_message(rig):
    _, iid, _ = play(rig)
    rows = msgs(rig, iid)
    by_agent = {}
    for r in rows:
        by_agent.setdefault(r["agent"], []).append(r)
    assert {a: len(v) for a, v in by_agent.items()} == {"triage": 3, "investigator": 4, "remediator": 2, "scribe": 1}
    for r in by_agent["investigator"] + by_agent["remediator"]:
        assert (r["model"], r["p"], r["c"]) == ("gpt-4.1", 90, 10) and r["cost"] == pytest.approx(STRONG)
    for r in by_agent["triage"] + by_agent["scribe"]:
        assert (r["model"], r["p"], r["c"]) == ("gpt-4.1-mini", 90, 10) and r["cost"] == pytest.approx(FAST)


def test_incident_totals_equal_the_sum_of_its_calls_and_resolution_cost_excludes_the_scribe(rig):
    _, iid, res = play(rig)
    resolve_cost = 6 * STRONG + 3 * FAST            # triage 3, investigator 4, remediator 2 -> before the Scribe
    assert res.cost_usd == pytest.approx(resolve_cost)
    view = ep.incident_view(rig.g, iid)
    assert view["cost_usd"] == pytest.approx(sum(r["cost"] for r in msgs(rig, iid)))
    assert view["cost_usd"] > res.cost_usd          # the postmortem call is not part of the cost to resolve
    assert view["cost_usd"] == pytest.approx(resolve_cost + FAST)


def test_each_decided_action_links_to_the_turn_that_caused_it_system_steps_do_not(rig):
    _, iid, _ = play(rig)
    rows = rig.g.rows("MATCH (i:Incident {id:$i})-[t:TOOK]->(a:Action) OPTIONAL MATCH (m:Message)-[:LED_TO]->(a) "
                      "RETURN t.seq AS seq, a.tool AS tool, a.agent AS agent, count(m) AS led ORDER BY seq", {"i": iid})
    led = {(r["agent"], r["tool"]): r["led"] for r in rows}
    assert led[("triage", "recall_similar")] == 0                    # run by the coordinator, not decided by the LLM
    assert led[("scribe", "consolidate")] == 0 and led[("scribe", "link_people")] == 0
    assert led[("triage", "blast_radius")] == led[("investigator", "rank_suspects")] == 1
    assert led[("remediator", "rollback")] == led[("scribe", "write_postmortem")] == 1


def test_explain_action_gives_reasoning_evidence_and_a_highlightable_subgraph(rig):
    _, iid, _ = play(rig)
    rollback = next(a for a in ep.incident_view(rig.g, iid)["actions"] if a["tool"] == "rollback")
    ex = trace.explain_action(rig.g, rollback["id"])
    assert ex["action"]["tool"] == "rollback" and ex["action"]["agent"] == "remediator"
    assert ex["action"]["args"] == {"service": "checkout-api"} and ex["action"]["outcome"] == "success"
    assert ex["system_initiated"] is False
    assert ex["reasoning"]["model"] == "gpt-4.1" and ex["reasoning"]["cost_usd"] == pytest.approx(STRONG)
    assert [(e["label"], e["name"], e["why"]) for e in ex["evidence"]] == [("Service", "checkout-api",
                                                                           "remediation target")]
    sg = ex["subgraph"]
    kinds = {e["type"] for e in sg["edges"]}
    assert {"TOOK", "BY", "LED_TO", "CITED"} <= kinds
    ids = {n["id"] for n in sg["nodes"]}
    assert rollback["id"] in ids and iid in ids and all(e["from"] in ids and e["to"] in ids for e in sg["edges"])
    assert set(ex["highlight"]) == ids and ex["memory_path"] is None          # cold start: no memory behind it


def test_explain_action_shows_the_memory_path_behind_a_fast_path_action(rig):
    play(rig)                                                        # incident 1 teaches the playbook
    plans = std_plans()
    plans["investigator"] = [[("rank_suspects", {})]]
    _, iid, res = play(rig, plans, service="refund-api")
    assert res.fast_path
    rollback = next(a for a in ep.incident_view(rig.g, iid)["actions"] if a["tool"] == "rollback")
    ex = trace.explain_action(rig.g, rollback["id"])
    path = ex["memory_path"]
    assert path["playbook"]["name"] == "bad_deploy: rollback"
    assert len(path["similar_incidents"]) == 1 and path["similar_incidents"][0]["basis"] in ("both", "structure")
    assert path["step"]["step"] == 1
    kinds = {e["type"] for e in ex["subgraph"]["edges"]}
    assert {"SIMILAR_TO", "RESOLVED_BY", "HAS_STEP"} <= kinds       # Incident -> similar -> Playbook -> Step -> Action
    labels = {n["label"] for n in ex["subgraph"]["nodes"]}
    assert {"Incident", "Playbook", "Step", "Action", "Service"} <= labels


def test_explaining_a_coordinator_step_says_so_and_unknown_ids_return_none(rig):
    _, iid, _ = play(rig)
    recall = next(a for a in ep.incident_view(rig.g, iid)["actions"] if a["tool"] == "recall_similar")
    ex = trace.explain_action(rig.g, recall["id"])
    assert ex["system_initiated"] is True and ex["reasoning"] is None and "coordinator" in ex["note"]
    assert trace.explain_action(rig.g, "nope") is None and trace.incident_trace(rig.g, "nope") is None


def test_trace_timeline_is_ordered_places_handoffs_between_agents_and_totals_match(rig):
    _, iid, res = play(rig)
    tr = trace.incident_trace(rig.g, iid)
    tl = tr["timeline"]
    assert tl[0]["type"] == "action" and tl[0]["tool"] == "recall_similar" and tl[0]["agent"] == trace.SYSTEM
    actions = [e for e in tl if e["type"] == "action"]
    assert [e["seq"] for e in actions] == sorted(e["seq"] for e in actions)           # actions in execution order
    # every decided action is immediately preceded by (or follows within its block) the llm turn that caused it
    for e in actions:
        if e["led_by"]:
            assert tl.index(next(x for x in tl if x["type"] == "llm" and x["id"] == e["led_by"])) < tl.index(e)
    hand = [(i, e) for i, e in enumerate(tl) if e["type"] == "handoff"]
    assert [(e["from"], e["to"]) for _, e in hand] == [("triage", "investigator"), ("investigator", "remediator")]
    for i, e in hand:
        before = [x for x in tl[:i] if x["type"] != "handoff"][-1]
        after = [x for x in tl[i + 1:] if x["type"] != "handoff"][0]
        assert (before.get("acting_agent") or before["agent"]) == e["from"]
        assert (after.get("acting_agent") or after["agent"]) == e["to"]
    # cost accounting is internally consistent and priced
    c = tr["cost"]
    assert c["llm_calls"] == 10 and c["unpriced_calls"] == 0
    assert c["total_usd"] == pytest.approx(6 * STRONG + 4 * FAST) and c["recorded_usd"] == pytest.approx(c["total_usd"])
    assert set(c["by_agent"]) == {"triage", "investigator", "remediator", "scribe"}
    assert c["by_agent"]["investigator"]["cost_usd"] == pytest.approx(4 * STRONG)
    assert set(c["by_model"]) == {"gpt-4.1", "gpt-4.1-mini"}
    assert sum(b["cost_usd"] for b in c["by_model"].values()) == pytest.approx(c["total_usd"])
    assert tr["outcome"]["cost_to_resolve"] == pytest.approx(res.cost_usd) and tr["outcome"]["steps_to_resolve"] == 8


def test_trace_of_a_fast_path_incident_lists_the_memory_it_used_and_costs_less(rig):
    _, first, r1 = play(rig)
    plans = std_plans()
    plans["investigator"] = [[("rank_suspects", {})]]
    _, second, r2 = play(rig, plans, service="refund-api")
    tr = trace.incident_trace(rig.g, second)
    assert tr["outcome"]["fast_path"] and [m["incident"] for m in tr["memory_used"]] == [first]
    assert tr["memory_used"][0]["playbook_name"] == "bad_deploy: rollback"
    assert r2.cost_usd < r1.cost_usd                                 # skipping the Investigator is cheaper
    assert "investigator" not in tr["cost"]["by_agent"]


def test_markdown_export_reads_as_a_story(rig):
    play(rig)
    plans = std_plans()
    plans["investigator"] = [[("rank_suspects", {})]]
    _, iid, _ = play(rig, plans, service="refund-api")
    md = trace.render_markdown(trace.incident_trace(rig.g, iid))
    assert md.startswith("# Incident trace: refund-api: error_rate alert")
    for needle in ("**fast path**", "## Memory used", "bad_deploy: rollback", "## Cost by agent", "| remediator |",
                   "## Timeline", "**handoff** triage -> remediator", "`rollback(service=refund-api)` -> **success**",
                   "coordinator:", "cost to resolve"):
        assert needle in md, needle


def test_a_declined_fast_path_is_visible_in_the_trace(rig, monkeypatch):
    from api import config
    play(rig)
    monkeypatch.setattr(config, "FAST_PATH_MIN_STRUCTURE", 0.0)
    step = lambda e: rig.w.truth(e.iid)["steps"][0]
    plans = std_plans()
    plans["remediator"] = [[("recent_changes", lambda e: {"service": e.origin})],
                           [("set_flag", lambda e: {"key": step(e)["target"], "value": step(e)["value"]}),
                            ("check_health", lambda e: {"service": e.origin})]]
    plans["investigator"] = [[("rank_suspects", {})]]
    _, iid, _ = play(rig, plans, fault="bad_flag", service="fraud-scorer")
    tr = trace.incident_trace(rig.g, iid)
    assert tr["outcome"]["fast_path"] is None and tr["outcome"]["fast_path_declined"]
    assert "declined" in trace.render_markdown(tr)
