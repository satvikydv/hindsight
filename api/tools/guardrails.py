"""Action guardrails (PRD §11). They run before any remediation tool and return a refusal reason, or None to allow.

1. out_of_scope: the Remediator may only act on nodes in the incident's scope (blast radius, what the origin depends on).
2. One restart per tier-critical service per incident.
3. restart(api-gateway) and any failover need a hypothesis pointing at the target with score >= 0.7.

Refusals are recorded as Actions with outcome `error`, so they show up in the timeline and the path view.
"""

import json

from api.tools.base import Tool, ToolContext

MIN_HYPOTHESIS_SCORE = 0.7
GATEWAY = "api-gateway"
# remediation tools and the argument holding their target
TARGET_ARG = {"rollback": "service", "restart": "service", "failover": "service", "renew_cert": "service",
              "scale": "target", "set_flag": "key"}


def _target(tool: Tool, args: dict) -> str:
    return args[TARGET_ARG[tool.name]]


def _owner_service(ctx: ToolContext, flag_key: str) -> str | None:
    rows = ctx.g.read("MATCH (s:Service)-[h:HAS_FLAG]->(:ConfigFlag {key: $k}) WHERE h.valid_to IS NULL RETURN s.name",
                      {"k": flag_key})
    return rows[0][0] if rows else None


def _tier(ctx: ToolContext, name: str) -> str | None:
    rows = ctx.g.read("MATCH (s:Service {name: $n}) RETURN s.tier", {"n": name})
    return rows[0][0] if rows else None


def _has_strong_hypothesis(ctx: ToolContext, name: str) -> bool:
    rows = ctx.g.read(
        "MATCH (h:Hypothesis {incident_id: $iid})-[:POINTS_TO]->(t) "
        "WHERE t.name = $n AND h.score >= $min AND h.status <> 'rejected' RETURN count(h)",
        {"iid": ctx.iid, "n": name, "min": MIN_HYPOTHESIS_SCORE})
    return rows[0][0] > 0


def _restarts_so_far(ctx: ToolContext, name: str) -> int:
    rows = ctx.g.read(
        "MATCH (:Incident {id: $iid})-[:TOOK]->(a:Action {tool: 'restart'}) "
        "WHERE a.outcome IN ['success', 'no_effect', 'harmful'] RETURN a.args_json", {"iid": ctx.iid})
    return sum(1 for (blob,) in rows if json.loads(blob).get("service") == name)


def check(ctx: ToolContext, tool: Tool, args: dict) -> str | None:
    target = _target(tool, args)
    node = _owner_service(ctx, target) if tool.name == "set_flag" else target
    if node is None:
        return f"out_of_scope: no current owner for flag {target!r}"
    if node not in ctx.scope():
        return f"out_of_scope: {node!r} is not in this incident's blast radius or dependencies"

    if tool.name == "restart" and _tier(ctx, node) == "critical" and _restarts_so_far(ctx, node) >= 1:
        return f"guardrail: {node!r} is tier-critical and was already restarted once in this incident"
    needs_confidence = tool.name == "failover" or (tool.name == "restart" and node == GATEWAY)
    if needs_confidence and not _has_strong_hypothesis(ctx, node):
        return (f"guardrail: {tool.name}({node}) needs a hypothesis pointing at it with score >= "
                f"{MIN_HYPOTHESIS_SCORE}; propose one with evidence first")
    return None
