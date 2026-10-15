"""Traceability: for any incident, what happened, why each action was taken, and what it cost.

- `explain_action`: the answer to "why did the crew do that?": the LLM turn that decided it, the nodes it cited and why,
  the memory path behind it (similar past incident -> playbook -> step), and the subgraph to highlight in the UI (PRD §10).
- `incident_trace`: one ordered timeline per incident (actions, LLM calls, handoffs) with cost and latency by agent/model.
- `render_markdown`: the same trace as a document, for the README, the demo and postmortems.

Everything is read from the graph, so a trace survives restarts and can be rebuilt for any past incident.
"""

import json

from api.graph import queries as q
from api.graph.client import TenantGraph
from api.memory import episodic as ep

MAX_SUBGRAPH_NODES = 300
SYSTEM = "system"   # actions the coordinator ran itself (memory lookup at start, the Scribe's consolidation)


def _display(label: str, props: dict) -> str:
    for key in ("name", "key", "version", "title", "text"):
        if props.get(key):
            return str(props[key])[:80]
    return props.get("id", label)


def explain_action(g: TenantGraph, action_id: str) -> dict | None:
    """Everything that justifies one action, as plain data plus a highlightable subgraph."""
    head = g.rows("MATCH (i:Incident)-[t:TOOK]->(a:Action {id: $aid}) "
                  "RETURN i.id AS incident, i.fast_path_playbook AS fast_path, i.fast_path_declined AS declined, "
                  "t.seq AS seq", {"aid": action_id})
    if not head:
        return None
    iid, fast_path, declined = head[0]["incident"], head[0]["fast_path"], head[0]["declined"]

    core = g.rows(q.Q11_EXPLAIN_ACTION, {"aid": action_id})[0]
    props = core["a"].properties
    action = {"id": action_id, "seq": head[0]["seq"], "agent": core["agent"], "tool": props["tool"],
              "args": json.loads(props.get("args_json") or "{}"), "outcome": props.get("outcome"),
              "summary": props.get("result_summary"), "at": props.get("at"), "rationale": props.get("rationale")}

    nodes: dict[str, dict] = {action_id: {"id": action_id, "label": "Action", "name": f"{action['tool']}"},
                              iid: {"id": iid, "label": "Incident", "name": ""}}
    edges: list[dict] = [{"from": iid, "to": action_id, "type": "TOOK"}]
    if core["agent"]:
        nodes[f"agent:{core['agent']}"] = {"id": f"agent:{core['agent']}", "label": "Agent", "name": core["agent"]}
        edges.append({"from": action_id, "to": f"agent:{core['agent']}", "type": "BY"})

    evidence = []
    for ev in core["evidence"]:
        node = ev["node"]
        if node is None:
            continue
        label, p = node.labels[0], node.properties
        evidence.append({"label": label, "id": p["id"], "name": _display(label, p), "why": ev["why"]})
        nodes[p["id"]] = {"id": p["id"], "label": label, "name": _display(label, p)}
        edges.append({"from": action_id, "to": p["id"], "type": "CITED", "why": ev["why"]})

    reasoning = None
    m = g.rows("MATCH (m:Message)-[:LED_TO]->(:Action {id: $aid}) "
               "OPTIONAL MATCH (m)-[:LED_TO]->(sib:Action) "
               "RETURN m.id AS id, m.agent AS agent, m.content AS content, m.model AS model, "
               "m.prompt_tokens AS prompt_tokens, m.completion_tokens AS completion_tokens, "
               "m.cached_tokens AS cached_tokens, m.latency_ms AS latency_ms, m.cost_usd AS cost_usd, "
               "m.disk_cache AS disk_cache, count(sib) AS actions_in_turn", {"aid": action_id})
    if m:
        reasoning = m[0]
        nodes[reasoning["id"]] = {"id": reasoning["id"], "label": "Message", "name": f"{reasoning['agent']} turn"}
        edges.append({"from": reasoning["id"], "to": action_id, "type": "LED_TO"})

    cited_ids = [e["id"] for e in evidence]
    hypotheses = g.rows("MATCH (h:Hypothesis {incident_id: $iid})-[:POINTS_TO]->(n) WHERE n.id IN $ids "
                        "RETURN h.id AS id, h.text AS text, h.score AS score, h.status AS status, n.id AS target",
                        {"iid": iid, "ids": cited_ids}) if cited_ids else []
    for h in hypotheses:
        nodes[h["id"]] = {"id": h["id"], "label": "Hypothesis", "name": h["text"][:60]}
        edges.append({"from": h["id"], "to": h["target"], "type": "POINTS_TO"})

    memory_path = _memory_path(g, iid, action["tool"], fast_path or declined, evidence, nodes, edges)
    nodes[iid]["name"] = g.read("MATCH (i:Incident {id: $i}) RETURN i.title", {"i": iid})[0][0] or iid

    return {"action": action, "incident": iid, "reasoning": reasoning, "evidence": evidence,
            "supporting_hypotheses": hypotheses, "memory_path": memory_path,
            "system_initiated": reasoning is None,
            "note": None if reasoning else "run by the coordinator, not decided by an LLM turn",
            "subgraph": {"nodes": list(nodes.values())[:MAX_SUBGRAPH_NODES], "edges": edges},
            "highlight": list(nodes)}


def _memory_path(g, iid, tool, playbook_id, evidence, nodes, edges) -> dict | None:
    """Incident -> similar past incident -> Playbook -> Step: the recall path that justified a playbook-driven action."""
    cited_pb = next((e["id"] for e in evidence if e["label"] == "Playbook"), None)
    pid = playbook_id or cited_pb
    if not pid:
        return None
    rows = g.rows("MATCH (i:Incident {id: $iid})-[s:SIMILAR_TO]->(p:Incident)-[:RESOLVED_BY]->(pb:Playbook {id: $pid}) "
                  "RETURN p.id AS incident, p.title AS title, s.score AS score, s.basis AS basis, "
                  "pb.name AS playbook, pb.weight AS weight, pb.status AS status", {"iid": iid, "pid": pid})
    step = g.rows("MATCH (:Playbook {id: $pid})-[:HAS_STEP]->(s:Step {tool: $tool}) "
                  "RETURN s.id AS id, s.order AS step, s.args_template AS args LIMIT 1", {"pid": pid, "tool": tool})
    pb = g.rows("MATCH (pb:Playbook {id: $pid}) RETURN pb.name AS name, pb.weight AS weight, pb.status AS status",
                {"pid": pid})
    if not pb:
        return None
    nodes[pid] = {"id": pid, "label": "Playbook", "name": pb[0]["name"]}
    for r in rows:
        nodes[r["incident"]] = {"id": r["incident"], "label": "Incident", "name": r["title"] or r["incident"]}
        edges += [{"from": iid, "to": r["incident"], "type": "SIMILAR_TO", "why": f"score {r['score']} ({r['basis']})"},
                  {"from": r["incident"], "to": pid, "type": "RESOLVED_BY"}]
    if step:
        nodes[step[0]["id"]] = {"id": step[0]["id"], "label": "Step", "name": f"step {step[0]['step']}: {tool}"}
        edges.append({"from": pid, "to": step[0]["id"], "type": "HAS_STEP"})
    return {"playbook": {"id": pid, **pb[0]}, "similar_incidents": rows, "step": step[0] if step else None}


# ---- whole-incident trace ------------------------------------------------------------------------------------------
def incident_trace(g: TenantGraph, iid: str) -> dict | None:
    view = ep.incident_view(g, iid)
    if view is None:
        return None
    msgs = g.rows("MATCH (m:Message {role: 'assistant'})-[l:IN_LOG]->(:Incident {id: $iid}) "
                  "OPTIONAL MATCH (m)-[:LED_TO]->(a:Action) "
                  "RETURN m.id AS id, m.agent AS agent, m.content AS content, m.model AS model, "
                  "m.prompt_tokens AS prompt_tokens, m.completion_tokens AS completion_tokens, "
                  "m.cached_tokens AS cached_tokens, m.latency_ms AS latency_ms, m.cost_usd AS cost_usd, "
                  "m.disk_cache AS disk_cache, l.seq AS seq, collect(a.id) AS actions ORDER BY seq", {"iid": iid})
    actions = {a["id"]: a for a in view["actions"]}
    led_by = {aid: m["id"] for m in msgs for aid in m["actions"]}
    ordered = sorted(view["actions"], key=lambda a: a["seq"])

    timeline: list[dict] = []
    pending = [a for a in ordered if a["id"] not in led_by]         # system-initiated actions, in seq order

    def action_entry(a: dict) -> dict:
        cites = g.read("MATCH (:Action {id: $a})-[c:CITED]->() RETURN count(c)", {"a": a["id"]})[0][0]
        return {"type": "action", "seq": a["seq"], "agent": a["agent"] if a["id"] in led_by else SYSTEM,
                "acting_agent": a["agent"], "tool": a["tool"], "args": json.loads(a["args"] or "{}"),
                "outcome": a["outcome"],
                "summary": a["summary"], "id": a["id"], "cited": cites, "led_by": led_by.get(a["id"])}

    for m in msgs:
        mine = sorted((actions[x] for x in m["actions"] if x in actions), key=lambda a: a["seq"])
        first = mine[0]["seq"] if mine else None
        while pending and first is not None and pending[0]["seq"] < first:   # coordinator-run steps that came before
            timeline.append(action_entry(pending.pop(0)))
        timeline.append({"type": "llm", "agent": m["agent"], "id": m["id"], "model": m["model"],
                         "prompt_tokens": m["prompt_tokens"], "completion_tokens": m["completion_tokens"],
                         "cached_tokens": m["cached_tokens"], "latency_ms": m["latency_ms"], "cost_usd": m["cost_usd"],
                         "disk_cache": m["disk_cache"], "text": (m["content"] or "")[:300]})
        timeline.extend(action_entry(a) for a in mine)
    timeline.extend(action_entry(a) for a in pending)

    timeline = _place_handoffs(timeline, view["handoffs"])

    by_agent: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    unpriced = 0
    for m in msgs:
        cost = m["cost_usd"]
        unpriced += cost is None
        tok = (m["prompt_tokens"] or 0) + (m["completion_tokens"] or 0)
        for bucket, key in ((by_agent, m["agent"]), (by_model, m["model"] or "unknown")):
            b = bucket.setdefault(key, {"calls": 0, "tokens": 0, "cost_usd": 0.0, "latency_ms": 0})
            b["calls"] += 1
            b["tokens"] += tok
            b["cost_usd"] = round(b["cost_usd"] + (cost or 0.0), 8)
            b["latency_ms"] += m["latency_ms"] or 0
    total = round(sum(b["cost_usd"] for b in by_agent.values()), 8)

    meta = g.rows("MATCH (i:Incident {id: $iid}) RETURN i.fast_path_playbook AS fast_path, "
                  "i.fast_path_declined AS declined, i.steps_to_resolve AS steps_to_resolve, "
                  "i.tokens_to_resolve AS tokens_to_resolve, i.cost_to_resolve AS cost_to_resolve, "
                  "i.cost_usd AS cost_usd, i.llm_ms AS llm_ms, i.escalation_team AS escalation_team, "
                  "i.unconfirmed_root_cause AS unconfirmed_root_cause", {"iid": iid})[0]
    recalled = g.rows("MATCH (:Incident {id: $iid})-[s:SIMILAR_TO]->(p:Incident) "
                      "OPTIONAL MATCH (p)-[:RESOLVED_BY]->(pb:Playbook) "
                      "RETURN p.id AS incident, p.title AS title, s.score AS score, s.basis AS basis, "
                      "pb.id AS playbook, pb.name AS playbook_name ORDER BY score DESC", {"iid": iid})
    return {"incident": {k: view[k] for k in ("id", "title", "severity", "status", "reason", "steps", "tokens")},
            "outcome": {"fast_path": meta["fast_path"], "fast_path_declined": meta["declined"],
                        "steps_to_resolve": meta["steps_to_resolve"], "tokens_to_resolve": meta["tokens_to_resolve"],
                        "cost_to_resolve": meta["cost_to_resolve"], "escalated_to": meta["escalation_team"],
                        "unconfirmed_root_cause": bool(meta["unconfirmed_root_cause"])},
            "memory_used": recalled, "root_causes": view["root_causes"], "timeline": timeline,
            "cost": {"total_usd": total, "recorded_usd": meta["cost_usd"], "llm_calls": len(msgs),
                     "unpriced_calls": unpriced, "llm_ms": sum(m["latency_ms"] or 0 for m in msgs),
                     "by_agent": by_agent, "by_model": by_model}}


def _place_handoffs(timeline: list[dict], handoffs: list[dict]) -> list[dict]:
    """A handoff sits just before the first event of the agent that received it, after the previous handoff."""
    out = list(timeline)
    floor = 0
    for h in handoffs:                                   # already ordered by seq
        idx = next((i for i in range(floor, len(out)) if out[i]["type"] != "handoff"
                    and (out[i].get("acting_agent") or out[i].get("agent")) == h["to"]), len(out))
        out.insert(idx, {"type": "handoff", "from": h["from"], "to": h["to"], "summary": h["summary"]})
        floor = idx + 1
    return out


def render_markdown(trace: dict) -> str:
    inc, out, cost = trace["incident"], trace["outcome"], trace["cost"]
    L = [f"# Incident trace: {inc['title']}", "",
         f"- status **{inc['status']}** ({inc['reason'] or 'n/a'}), severity {inc['severity']}",
         f"- steps to resolve **{out['steps_to_resolve']}**, tokens {out['tokens_to_resolve']}, "
         f"cost to resolve **${(out['cost_to_resolve'] or 0):.4f}** (whole incident ${cost['total_usd']:.4f}, "
         f"{cost['llm_calls']} LLM calls, {cost['llm_ms']} ms)"]
    if out["fast_path"]:
        L.append(f"- **fast path**: followed playbook `{out['fast_path']}` from memory")
    if out["fast_path_declined"]:
        L.append(f"- fast path **declined**: the Remediator checked the evidence and the playbook did not apply "
                 f"(`{out['fast_path_declined']}`)")
    if out["unconfirmed_root_cause"]:
        L.append("- resolved **without a confirmed root cause**")
    if trace["memory_used"]:
        L += ["", "## Memory used", ""]
        L += [f"- {m['title']} (similarity {m['score']}, matched by {m['basis']})"
              + (f", playbook *{m['playbook_name']}*" if m["playbook_name"] else "") for m in trace["memory_used"]]
    L += ["", "## Cost by agent", "", "| agent | calls | tokens | cost | latency |", "|---|---|---|---|---|"]
    L += [f"| {a} | {b['calls']} | {b['tokens']} | ${b['cost_usd']:.4f} | {b['latency_ms']} ms |"
          for a, b in cost["by_agent"].items()]
    L += ["", "## Timeline", ""]
    for e in trace["timeline"]:
        if e["type"] == "handoff":
            L.append(f"- **handoff** {e['from']} -> {e['to']}: {e['summary']}")
        elif e["type"] == "llm":
            c = f"${e['cost_usd']:.5f}" if e["cost_usd"] is not None else "unpriced"
            L.append(f"- [{e['agent']}] LLM turn ({e['model']}, {e['prompt_tokens']}+{e['completion_tokens']} tokens, "
                     f"{c}, {e['latency_ms']} ms)")
        else:
            args = ", ".join(f"{k}={v}" for k, v in e["args"].items())
            who = e["agent"] if e["agent"] != SYSTEM else "coordinator"
            bullet = "-" if e["agent"] == SYSTEM else "  -"      # steps an LLM turn decided nest under that turn
            L.append(f"{bullet} #{e['seq']} {who}: `{e['tool']}({args})` -> **{e['outcome']}** {e['summary'] or ''}"[:260])
    return "\n".join(L) + "\n"
