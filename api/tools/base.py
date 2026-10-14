"""Tool plumbing. Every tool call, from any agent, goes through `execute`:

  validate args -> write Action (pending) -> guardrails -> run -> CITED edges -> finish Action -> wrap result

Agents never see or write Cypher. A tool is a fixed, parameterised function; the tenant graph and incident come from
the ToolContext (server-side), never from model output. Results are returned wrapped as untrusted data (PRD §11).
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from api.graph import queries as q
from api.graph.client import TenantGraph
from api.ids import new_id
from api.llm import Embedder
from api.memory import episodic as ep
from api.sim.world import World

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 4000
JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


@dataclass
class ToolContext:
    g: TenantGraph
    world: World
    iid: str
    mode: str = "graph"                    # memory ablation: off | vector | graph
    embedder: Embedder | None = None
    agent: str = ""                        # the agent whose tool call is running; set by execute()
    _origin: str | None = field(default=None, repr=False)
    _scope: set[str] | None = field(default=None, repr=False)

    @property
    def now(self) -> int:
        return self.world.now_ms           # all timestamps come from the server clock, never from the model

    def origin(self) -> str:
        if self._origin is None:
            rows = self.g.read("MATCH (:Incident {id: $iid})-[:AFFECTS {role: 'origin'}]->(s:Service) RETURN s.name",
                               {"iid": self.iid})
            if not rows:
                raise LookupError(f"incident {self.iid} has no origin service")
            self._origin = rows[0][0]
        return self._origin

    def scope(self) -> set[str]:
        """Names the Remediator may act on, purely structural: the origin's blast radius, what the origin depends on
        within 2 hops, and the direct callers of those dependencies (so a datastore fix can restart its top caller).
        Unrelated alerts (red herrings) are deliberately not in scope."""
        if self._scope is None:
            o = self.origin()
            hood = {r["name"] for r in self.g.rows(q.Q18_NEIGHBOURHOOD, {"svc": o})}
            names = hood | {r["service"] for r in self.g.rows(q.Q1_BLAST_RADIUS, {"svc": o})}
            names |= {r[0] for r in self.g.read(
                "MATCH (c:Service)-[r:DEPENDS_ON]->(n) WHERE n.name IN $hood AND r.valid_to IS NULL RETURN c.name",
                {"hood": sorted(hood)})}
            self._scope = names
        return self._scope

    def refs(self, names: list[str], why: str) -> list[tuple[str, str, str]]:
        """(label, id, why) for Service/Datastore names, for CITED edges."""
        names = sorted(set(names))
        if not names:
            return []
        rows = self.g.read("MATCH (n) WHERE (n:Service OR n:Datastore) AND n.name IN $names "
                           "RETURN labels(n)[0], n.id", {"names": names})
        return [(label, nid, why) for label, nid in rows]


@dataclass
class Tool:
    name: str
    description: str
    properties: dict                       # JSON-schema properties
    required: list[str]
    agents: set[str]
    fn: Callable[..., dict]                # fn(ctx, **args) -> result dict; may set result["_outcome"]
    cites: Callable[[ToolContext, dict, dict], list[tuple[str, str, str]]] | None = None
    acts: bool = False                     # remediation: guardrails apply

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.properties, "required": self.required,
                           "additionalProperties": False}}}


@dataclass
class ToolOutcome:
    name: str
    outcome: str                           # success | no_effect | harmful | error
    result: dict
    text: str                              # what goes back to the model
    action_id: str
    counted: bool = True                   # counts toward the step budget


REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    if tool.name in REGISTRY:
        raise ValueError(f"duplicate tool {tool.name}")
    REGISTRY[tool.name] = tool
    return tool


def schemas_for(agent: str) -> list[dict]:
    return [t.schema() for t in REGISTRY.values() if agent in t.agents]


def validate(tool: Tool, args: dict) -> str | None:
    """A small JSON-schema check: required keys, no unknown keys, types, enums. Returns an error message or None."""
    unknown = set(args) - set(tool.properties)
    if unknown:
        return f"unknown argument(s) {sorted(unknown)}; allowed: {sorted(tool.properties)}"
    for key in tool.required:
        if key not in args:
            return f"missing required argument {key!r}"
    for key, val in args.items():
        spec = tool.properties[key]
        want = JSON_TYPES[spec["type"]]
        if isinstance(val, bool) and spec["type"] in ("integer", "number"):
            return f"argument {key!r} must be {spec['type']}"
        if not isinstance(val, want):
            return f"argument {key!r} must be {spec['type']}"
        if "enum" in spec and val not in spec["enum"]:
            return f"argument {key!r} must be one of {spec['enum']}"
    return None


def wrap_untrusted(name: str, payload: dict) -> str:
    """Tool output is data, never instructions: log lines can carry planted prompt injection."""
    body = json.dumps(payload, default=str, separators=(",", ":"))
    if len(body) > MAX_RESULT_CHARS:
        body = body[:MAX_RESULT_CHARS] + '..."truncated"'
    return f'<tool_result untrusted="true" tool="{name}">{body}</tool_result>'


def _summary(result: dict) -> str:
    return json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, default=str,
                      separators=(",", ":"))[:300]


def execute(ctx: ToolContext, agent: str, name: str, args: dict, seq: int, rationale: str = "") -> ToolOutcome:
    """Run one tool call end to end. Always leaves an Action in the graph, even for rejected or failed calls."""
    from api.tools import guardrails

    action_id = new_id()
    tool = REGISTRY.get(name)
    ctx.agent = agent
    ep.begin_action(ctx.g, ctx.iid, action_id, agent, name, args, seq, ctx.now, rationale)

    def fail(msg: str, outcome: str = "error") -> ToolOutcome:
        ep.finish_action(ctx.g, action_id, outcome, msg, ctx.now)
        ep.add_usage(ctx.g, ctx.iid, steps=1)
        payload = {"error": msg}
        return ToolOutcome(name, outcome, payload, wrap_untrusted(name, payload), action_id)

    if tool is None or agent not in tool.agents:
        return fail(f"unknown tool {name!r}; available: {sorted(n for n, t in REGISTRY.items() if agent in t.agents)}")
    bad = validate(tool, args)
    if bad:
        return fail(bad)
    if tool.acts:
        refusal = guardrails.check(ctx, tool, args)
        if refusal:
            return fail(refusal)
    try:
        result = tool.fn(ctx, **args)
    except (ValueError, LookupError) as e:       # unknown service, missing node: the model's mistake, not ours
        return fail(str(e))
    except Exception as e:                       # a real bug: record it on the incident, do not kill the crew
        log.exception("tool %s crashed", name)
        return fail(f"internal error in {name}: {type(e).__name__}")

    outcome = result.pop("_outcome", "success")
    if tool.cites:
        for label, node_id, why in tool.cites(ctx, args, result):
            try:
                ep.cite(ctx.g, action_id, label, node_id, why)
            except ValueError:
                log.warning("skipped citation of %s", label)
    ep.finish_action(ctx.g, action_id, outcome, _summary(result), ctx.now)
    ep.add_usage(ctx.g, ctx.iid, steps=1)
    return ToolOutcome(name, outcome, result, wrap_untrusted(name, result), action_id)
