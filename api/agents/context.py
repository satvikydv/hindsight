"""What an agent sees when its turn starts. Everything comes from the graph (PRD §7: agents never pass context to each
other in prompts), so any agent can be swapped, or the server restarted, mid-incident.

Memory ablation (PRD §12): `graph` shows structured episodes, cause types and playbooks; `vector` shows only the
postmortem text of text-similar past incidents; `off` shows no past incidents at all.
"""

import json

from api.agents.intake import strength
from api.memory import episodic as ep
from api.memory import procedural as pr
from api.tools.base import ToolContext

MAX_ACTIONS = 15
MAX_SYMPTOMS = 25
POSTMORTEM_SNIPPET = 400


def _recalled(ctx: ToolContext) -> list[str]:
    if ctx.mode == "off":
        return []
    rows = ctx.g.rows(
        "MATCH (:Incident {id: $iid})-[s:SIMILAR_TO]->(p:Incident) "
        "OPTIONAL MATCH (p)-[:RESOLVED_BY]->(pb:Playbook) "
        "OPTIONAL MATCH (p)-[:ROOT_CAUSE]->(rc) "
        "RETURN p.id AS id, p.title AS title, s.score AS score, s.basis AS basis, p.postmortem AS postmortem, "
        "pb.id AS pb_id, pb.name AS pb_name, pb.weight AS pb_weight, pb.status AS pb_status, "
        "collect(DISTINCT labels(rc)[0]) AS causes ORDER BY score DESC", {"iid": ctx.iid})
    out = []
    for r in rows:
        head = f"- {r['id']} \"{r['title']}\" (similarity {r['score']}, matched by {r['basis']})"
        if ctx.mode == "vector":
            pm = (r["postmortem"] or "no postmortem recorded")[:POSTMORTEM_SNIPPET]
            out.append(f"{head}\n    postmortem: {pm}")
            continue
        extra = f" root cause type: {', '.join(r['causes']) or 'unknown'}"
        if r["pb_id"] and r["pb_status"] == "active":
            steps = pr.steps_of(ctx.g, r["pb_id"])
            seq = " -> ".join(f"{s['tool']}({', '.join(f'{k}={v}' for k, v in s['args'].items())})" for s in steps)
            extra += f"; resolved by playbook {r['pb_id']} \"{r['pb_name']}\" (weight {round(r['pb_weight'], 2)}): {seq}"
        out.append(head + extra)
    return out


def build_context(ctx: ToolContext, agent: str, notes: str = "") -> str:
    v = ep.incident_view(ctx.g, ctx.iid)
    origin = ctx.origin()
    L = [f"INCIDENT {v['id']}: {v['title']} | severity {v['severity']} | status {v['status']} | "
         f"steps used so far {v['steps']}",
         f"origin service: {origin}",
         "affected: " + ", ".join(f"{s['name']} ({s['role']}, {s['tier']})" for s in v["services"])]

    # loudest first, so the cap drops the least informative symptoms, not whichever services sort last by name
    loudest = sorted(v["symptoms"], key=lambda s: -strength(s))[:MAX_SYMPTOMS]
    by_node: dict[str, list[str]] = {}
    for s in loudest:
        by_node.setdefault(s["service"], []).append(f"{s['kind']}={s['value']}")
    L.append("symptoms: " + "; ".join(f"{n}: {', '.join(k)}" for n, k in by_node.items()))

    if v["actions"]:
        L.append("actions so far:")
        for a in v["actions"][-MAX_ACTIONS:]:
            L.append(f"  #{a['seq']} {a['agent']} {a['tool']}({_args(a['args'])}) -> {a['outcome']}: "
                     f"{(a['summary'] or '')[:160]}")
    if v["hypotheses"]:
        L.append("hypotheses:")
        for h in v["hypotheses"]:
            L.append(f"  [{h['score']}] {h['text']} -> {h['target_label']} {h['target_id']} ({h['status']}) "
                     f"id={h['id']}")
    if v["root_causes"]:
        L.append("confirmed root cause(s): " + ", ".join(f"{r['label']} {r['id']}" for r in v["root_causes"]))
    if v["handoffs"]:
        L.append("handoffs: " + " | ".join(f"{h['from']} -> {h['to']}: {h['summary']}" for h in v["handoffs"]))

    recalled = _recalled(ctx)
    if recalled:
        L.append("similar past incidents (memory):")
        L.extend(recalled)
    elif ctx.mode != "off":
        L.append("similar past incidents (memory): none yet")
    if notes:
        L.append(notes)
    return "\n".join(L)


def _args(blob: str | None) -> str:
    try:
        return ", ".join(f"{k}={v}" for k, v in json.loads(blob or "{}").items())
    except json.JSONDecodeError:
        return ""
