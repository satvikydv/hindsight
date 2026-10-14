"""Procedural memory: Playbook -> Step chains with success counts (PRD §6, consolidation rules).

weight = (success + 1) / (attempt + 2), Laplace-smoothed, then halved for every 30 simulated days without use.
`base_weight` holds the smoothed value; `weight` = base_weight x decay, recomputed from base so decay never compounds.
"""

import json

from api.graph import queries as q
from api.graph.client import TenantGraph
from api.ids import new_id

DAY_MS = 86_400_000
HALF_LIFE_MS = 30 * DAY_MS
DEPRECATE_MIN_ATTEMPTS = 3
DEPRECATE_BELOW = 0.30


def _template(args_template: dict | str) -> str:
    return args_template if isinstance(args_template, str) else json.dumps(args_template, sort_keys=True)


def create_playbook(g: TenantGraph, name: str, fault_class: str, steps: list[dict], from_incident: str,
                    at: int) -> str:
    """New playbook distilled from a successful incident. steps: [{tool, args_template}], in order.
    The creating incident counts as one attempt and one success for the playbook and each of its steps."""
    pid = new_id()
    g.write("MATCH (i:Incident {id: $iid}) "
            "CREATE (pb:Playbook {id: $pid, name: $name, fault_class: $fc, success_count: 1, attempt_count: 1, "
            "base_weight: 2.0 / 3.0, weight: 2.0 / 3.0, last_used_at: $at, status: 'active', "
            "created_at: $at, source: 'agent'}) "
            "CREATE (i)-[:RESOLVED_BY]->(pb) CREATE (pb)-[:DERIVED_FROM]->(i)",
            {"iid": from_incident, "pid": pid, "name": name, "fc": fault_class, "at": at})
    g.write("MATCH (pb:Playbook {id: $pid}) UNWIND $steps AS s "
            "CREATE (st:Step {id: s.id, order: s.order, tool: s.tool, args_template: s.args, "
            "success_count: 1, attempt_count: 1, created_at: $at, source: 'agent'}) "
            "CREATE (pb)-[:HAS_STEP]->(st)",
            {"pid": pid, "at": at,
             "steps": [{"id": new_id(), "order": n, "tool": s["tool"], "args": _template(s["args_template"])}
                       for n, s in enumerate(steps, 1)]})
    g.write("MATCH (pb:Playbook {id: $pid})-[:HAS_STEP]->(a:Step) MATCH (pb)-[:HAS_STEP]->(b:Step) "
            "WHERE b.order = a.order + 1 CREATE (a)-[:NEXT]->(b)", {"pid": pid})
    return pid


def steps_of(g: TenantGraph, pid: str) -> list[dict]:
    """Q7. Each step: {step, tool, args (dict), wins, tries}."""
    rows = g.rows(q.Q7_PLAYBOOK_STEPS, {"pid": pid})
    for r in rows:
        r["args"] = json.loads(r["args"])
    return rows


def find_duplicate(g: TenantGraph, fault_class: str, steps: list[dict]) -> str | None:
    """An active playbook of the same fault_class with the identical tool + args_template sequence (merge rule)."""
    want = [(s["tool"], _template(s["args_template"])) for s in steps]
    rows = g.rows("MATCH (pb:Playbook {fault_class: $fc, status: 'active'})-[:HAS_STEP]->(s:Step) "
                  "RETURN pb.id AS pid, s.order AS n, s.tool AS tool, s.args_template AS args "
                  "ORDER BY pid, n", {"fc": fault_class})
    seqs: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        seqs.setdefault(r["pid"], []).append((r["tool"], r["args"]))
    return next((pid for pid, seq in seqs.items() if seq == want), None)


def reinforce(g: TenantGraph, pid: str, won: bool, at: int) -> dict:
    """Q13, then deprecate if it keeps failing. Returns {weight, status}."""
    g.write(q.Q13_REINFORCE_PLAYBOOK, {"pid": pid, "won": 1 if won else 0, "now": at})
    g.write("MATCH (pb:Playbook {id: $pid}) "
            "WHERE pb.attempt_count >= $min AND toFloat(pb.success_count) / pb.attempt_count < $below "
            "SET pb.status = 'deprecated'",
            {"pid": pid, "min": DEPRECATE_MIN_ATTEMPTS, "below": DEPRECATE_BELOW})
    return g.rows("MATCH (pb:Playbook {id: $pid}) RETURN pb.weight AS weight, pb.status AS status", {"pid": pid})[0]


def reinforce_steps(g: TenantGraph, pid: str, outcomes: dict[int, bool]) -> None:
    """Per-step counts. outcomes maps step order -> whether its Action succeeded."""
    g.write("MATCH (:Playbook {id: $pid})-[:HAS_STEP]->(s:Step) WHERE s.order IN $orders "
            "SET s.attempt_count = s.attempt_count + 1, "
            "s.success_count = s.success_count + CASE WHEN s.order IN $won THEN 1 ELSE 0 END",
            {"pid": pid, "orders": list(outcomes), "won": [n for n, ok in outcomes.items() if ok]})


def apply_decay(g: TenantGraph, now: int) -> int:
    """weight = base_weight x 0.5^(days unused / 30) for every active playbook. Idempotent for a given `now`."""
    rows = g.rows("MATCH (pb:Playbook {status: 'active'}) "
                  "RETURN pb.id AS id, pb.base_weight AS base, pb.last_used_at AS last")
    upd = [{"id": r["id"], "w": r["base"] * 0.5 ** (max(0, now - r["last"]) / HALF_LIFE_MS)} for r in rows]
    if upd:
        g.write("UNWIND $rows AS r MATCH (pb:Playbook {id: r.id}) SET pb.weight = r.w", {"rows": upd})
    return len(upd)


def candidates(g: TenantGraph, similar_incident_ids: list[str]) -> list[dict]:
    """Q6: active playbooks that resolved the similar incidents, best weight first."""
    return g.rows(q.Q6_CANDIDATE_PLAYBOOKS, {"similar_ids": similar_incident_ids})


def list_playbooks(g: TenantGraph) -> list[dict]:
    """For GET /playbooks: weight, counts, status and the incidents each was derived from."""
    return g.rows("MATCH (pb:Playbook) OPTIONAL MATCH (pb)-[:DERIVED_FROM]->(i:Incident) "
                  "RETURN pb.id AS id, pb.name AS name, pb.fault_class AS fault_class, pb.weight AS weight, "
                  "pb.success_count AS successes, pb.attempt_count AS attempts, pb.status AS status, "
                  "pb.last_used_at AS last_used_at, collect(i.id) AS derived_from ORDER BY weight DESC")
