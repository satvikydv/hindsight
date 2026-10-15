"""Episodic memory: what happened during an incident, written live, one short statement per step.

Rules from PRD §5/§6: an Action is written *before* the tool runs (outcome pending) and updated after, so a crash
leaves a trace; every write MERGEs on a client-generated id so retries never duplicate; every timestamp is passed in
by the server (`at`), never taken from the model; large text stays out of the graph.
"""

import json

from api.graph.client import TenantGraph
from api.graph import queries as q

ACTIVE_STATUSES = ("open", "investigating")  # an incident in one of these is resumable after a crash
TERMINAL_STATUSES = ("mitigated", "resolved", "failed")
CITE_LABELS = {"Service", "Datastore", "Deploy", "ConfigFlag", "Incident", "Playbook", "Fact", "Person", "Team",
               "Hypothesis", "Symptom", "Action"}
MAX_MESSAGE_CHARS = 2000


# ---- incident lifecycle ------------------------------------------------------------------------------------------
def open_incident(g: TenantGraph, iid: str, title: str, severity: str, at: int, origin: str,
                  impacted: list[str] | None = None) -> None:
    """Create the Incident with AFFECTS edges: one role=origin, the rest role=impacted."""
    g.write("MERGE (i:Incident {id: $iid}) "
            "ON CREATE SET i.title = $title, i.severity = $sev, i.status = 'open', i.opened_at = $at, "
            "i.steps = 0, i.tokens = 0, i.created_at = $at, i.source = 'agent'",
            {"iid": iid, "title": title, "sev": severity, "at": at})
    g.write("MATCH (i:Incident {id: $iid}) MATCH (s:Service {name: $svc}) "
            "MERGE (i)-[:AFFECTS {role: 'origin'}]->(s)", {"iid": iid, "svc": origin})
    for name in impacted or []:
        if name != origin:
            g.write("MATCH (i:Incident {id: $iid}) MATCH (s:Service {name: $svc}) "
                    "MERGE (i)-[:AFFECTS {role: 'impacted'}]->(s)", {"iid": iid, "svc": name})


def set_status(g: TenantGraph, iid: str, status: str, at: int, reason: str | None = None) -> None:
    g.write("MATCH (i:Incident {id: $iid}) SET i.status = $st, i.status_reason = $reason, "
            "i.resolved_at = CASE WHEN $terminal THEN $at ELSE i.resolved_at END",
            {"iid": iid, "st": status, "reason": reason, "at": at, "terminal": status in TERMINAL_STATUSES})


def set_severity(g: TenantGraph, iid: str, severity: str) -> None:
    g.write("MATCH (i:Incident {id: $iid}) SET i.severity = $sev", {"iid": iid, "sev": severity})


def set_fast_path(g: TenantGraph, iid: str, playbook_id: str) -> None:
    """Remember which playbook the coordinator told the Remediator to follow, for the Scribe to reinforce."""
    g.write("MATCH (i:Incident {id: $iid}) SET i.fast_path_playbook = $pid", {"iid": iid, "pid": playbook_id})


def decline_fast_path(g: TenantGraph, iid: str) -> None:
    """The Remediator checked the playbook's precondition, found it did not hold and took no action. Stop treating the
    playbook as followed, so the Scribe neither rewards nor penalises it (it was never tried)."""
    g.write("MATCH (i:Incident {id: $iid}) SET i.fast_path_declined = i.fast_path_playbook "
            "REMOVE i.fast_path_playbook", {"iid": iid})


def mark_resolution_cost(g: TenantGraph, iid: str) -> None:
    """Freeze steps and tokens at the moment the incident ends, before the Scribe adds its own: the learning curve
    measures the cost of resolving, not of writing it up."""
    g.write("MATCH (i:Incident {id: $iid}) SET i.steps_to_resolve = i.steps, i.tokens_to_resolve = i.tokens, "
            "i.cost_to_resolve = coalesce(i.cost_usd, 0.0)", {"iid": iid})


def add_usage(g: TenantGraph, iid: str, steps: int = 0, tokens: int = 0, cost: float = 0.0, llm_ms: int = 0) -> None:
    """Running step, token, dollar and LLM-latency totals, read back for the learning-curve chart and cost reports."""
    g.write("MATCH (i:Incident {id: $iid}) SET i.steps = i.steps + $steps, i.tokens = i.tokens + $tokens, "
            "i.cost_usd = coalesce(i.cost_usd, 0.0) + $cost, i.llm_ms = coalesce(i.llm_ms, 0) + $ms",
            {"iid": iid, "steps": steps, "tokens": tokens, "cost": cost or 0.0, "ms": llm_ms})


def set_signature(g: TenantGraph, iid: str, embedding: list[float]) -> None:
    g.write("MATCH (i:Incident {id: $iid}) SET i.signature_embedding = vecf32($vec)",
            {"iid": iid, "vec": embedding})


def add_symptoms(g: TenantGraph, iid: str, symptoms: list[dict]) -> None:
    """Each symptom: {service, kind, metric, value}. Id is derived from (incident, node, kind) so re-writes merge."""
    for s in symptoms:
        g.write("MATCH (i:Incident {id: $iid}) MATCH (t) WHERE (t:Service OR t:Datastore) AND t.name = $svc "
                "MERGE (y:Symptom {id: $sid}) SET y.kind = $kind, y.metric = $metric, y.value = $value "
                "MERGE (i)-[:SHOWED]->(y) MERGE (y)-[:OBSERVED_ON]->(t)",
                {"iid": iid, "svc": s["service"], "sid": f"{iid}:{s['service']}:{s['kind']}", "kind": s["kind"],
                 "metric": s.get("metric"), "value": s.get("value")})


# ---- actions -----------------------------------------------------------------------------------------------------
def begin_action(g: TenantGraph, iid: str, action_id: str, agent: str, tool: str, args: dict, seq: int, at: int,
                 rationale: str = "") -> None:
    """Write the Action with outcome=pending BEFORE the tool runs."""
    g.write("MATCH (i:Incident {id: $iid}) MATCH (ag:Agent {name: $agent}) "
            "MERGE (a:Action {id: $aid}) "
            "ON CREATE SET a.tool = $tool, a.args_json = $args, a.agent = $agent, a.at = $at, "
            "a.outcome = 'pending', a.rationale = $why, a.seq = $seq, a.created_at = $at, a.source = 'agent' "
            "MERGE (i)-[t:TOOK]->(a) SET t.seq = $seq "
            "MERGE (a)-[:BY]->(ag)",
            {"iid": iid, "aid": action_id, "agent": agent, "tool": tool, "args": json.dumps(args, sort_keys=True),
             "at": at, "why": rationale[:500], "seq": seq})


def cite(g: TenantGraph, action_id: str, label: str, node_id: str, why: str) -> None:
    """The explainability edge: the Action relied on this node. Label is whitelisted (it is spliced into Cypher)."""
    if label not in CITE_LABELS:
        raise ValueError(f"cannot cite a {label!r}")
    g.write(f"MATCH (a:Action {{id: $aid}}) MATCH (n:{label} {{id: $nid}}) "
            "MERGE (a)-[c:CITED]->(n) SET c.why = $why", {"aid": action_id, "nid": node_id, "why": why[:200]})


def finish_action(g: TenantGraph, action_id: str, outcome: str, summary: str, at: int) -> None:
    g.write("MATCH (a:Action {id: $aid}) SET a.outcome = $outcome, a.result_summary = $summary, a.finished_at = $at",
            {"aid": action_id, "outcome": outcome, "summary": summary[:500], "at": at})


def last_completed_action(g: TenantGraph, iid: str) -> dict | None:
    """Where to resume after a crash: the highest-seq Action that is no longer pending."""
    rows = g.rows("MATCH (:Incident {id: $iid})-[t:TOOK]->(a:Action) WHERE a.outcome <> 'pending' "
                  "RETURN a.id AS id, a.tool AS tool, a.outcome AS outcome, t.seq AS seq ORDER BY t.seq DESC LIMIT 1",
                  {"iid": iid})
    return rows[0] if rows else None


# ---- reasoning trail ---------------------------------------------------------------------------------------------
def add_hypothesis(g: TenantGraph, iid: str, hid: str, agent: str, text: str, score: float, at: int,
                   target_label: str, target_id: str) -> None:
    if target_label not in CITE_LABELS:
        raise ValueError(f"hypothesis cannot point to a {target_label!r}")
    g.write(f"MATCH (ag:Agent {{name: $agent}}) MATCH (t:{target_label} {{id: $tid}}) "
            "MERGE (h:Hypothesis {id: $hid}) "
            "ON CREATE SET h.incident_id = $iid, h.text = $text, h.status = 'open', h.created_at = $at, "
            "h.source = 'agent' "
            "SET h.score = $score "
            "MERGE (ag)-[:PROPOSED]->(h) MERGE (h)-[:POINTS_TO]->(t)",
            {"agent": agent, "tid": target_id, "hid": hid, "iid": iid, "text": text[:300], "score": score, "at": at})


def update_hypothesis(g: TenantGraph, hid: str, status: str, score: float | None = None) -> None:
    g.write("MATCH (h:Hypothesis {id: $hid}) SET h.status = $st, h.score = coalesce($score, h.score)",
            {"hid": hid, "st": status, "score": score})


def confirm_root_cause(g: TenantGraph, iid: str, label: str, node_id: str, confirmed_by: str) -> None:
    if label not in CITE_LABELS:
        raise ValueError(f"root cause cannot be a {label!r}")
    g.write(f"MATCH (i:Incident {{id: $iid}}) MATCH (n:{label} {{id: $nid}}) "
            "MERGE (i)-[r:ROOT_CAUSE]->(n) SET r.confirmed_by = $by", {"iid": iid, "nid": node_id, "by": confirmed_by})


def add_message(g: TenantGraph, iid: str, msg_id: str, agent: str, role: str, content: str, seq: int,
                at: int, usage: dict | None = None) -> None:
    """One LLM turn, kept so a crashed incident can reload its conversation from the graph. `usage` (assistant messages
    only) is the call's telemetry: model, tokens, latency, cost. That is what makes every call traceable and priceable."""
    u = usage or {}
    g.write("MATCH (i:Incident {id: $iid}) "
            "MERGE (m:Message {id: $mid}) "
            "ON CREATE SET m.agent = $agent, m.role = $role, m.content = $content, m.at = $at, "
            "m.created_at = $at, m.source = 'agent', m.model = $model, m.prompt_tokens = $pt, "
            "m.completion_tokens = $ct, m.cached_tokens = $cached, m.latency_ms = $ms, m.cost_usd = $cost, "
            "m.disk_cache = $disk "
            "MERGE (m)-[l:IN_LOG]->(i) SET l.seq = $seq",
            {"iid": iid, "mid": msg_id, "agent": agent, "role": role, "content": content[:MAX_MESSAGE_CHARS],
             "at": at, "seq": seq, "model": u.get("model"), "pt": u.get("prompt_tokens"),
             "ct": u.get("completion_tokens"), "cached": u.get("cached_tokens"), "ms": u.get("latency_ms"),
             "cost": u.get("cost_usd"), "disk": u.get("disk_cache")})


def link_message_action(g: TenantGraph, msg_id: str, action_id: str) -> None:
    """(Message)-[:LED_TO]->(Action): the LLM turn whose reasoning produced this action."""
    g.write("MATCH (m:Message {id: $mid}) MATCH (a:Action {id: $aid}) MERGE (m)-[:LED_TO]->(a)",
            {"mid": msg_id, "aid": action_id})


def load_messages(g: TenantGraph, iid: str, n: int = 10) -> list[dict]:
    """The last n messages, oldest first."""
    rows = g.rows("MATCH (m:Message)-[l:IN_LOG]->(:Incident {id: $iid}) "
                  "RETURN m.agent AS agent, m.role AS role, m.content AS content, l.seq AS seq "
                  "ORDER BY l.seq DESC LIMIT $n", {"iid": iid, "n": n})
    return rows[::-1]


# ---- coordination ------------------------------------------------------------------------------------------------
def write_handoff(g: TenantGraph, iid: str, frm: str, to: str, seq: int, summary: str, at: int) -> None:
    """HANDOFF edge between agents. The receiver loads context from the graph, not from the sender's prompt."""
    g.write("MATCH (a:Agent {name: $frm}) MATCH (b:Agent {name: $to}) "
            "MERGE (a)-[h:HANDOFF {incident_id: $iid, seq: $seq}]->(b) SET h.summary = $summary, h.at = $at",
            {"frm": frm, "to": to, "iid": iid, "seq": seq, "summary": summary[:300], "at": at})


def handoffs(g: TenantGraph, iid: str) -> list[dict]:
    return g.rows(q.Q16_HANDOFF_CHAIN, {"iid": iid})


def acquire_lock(g: TenantGraph, iid: str, agent: str, now: int, ttl_ms: int = 30_000) -> bool:
    """True if `agent` now holds the incident lock (acquired or refreshed). False: back off and retry."""
    return bool(g.write(q.Q15_ACQUIRE_LOCK, {"iid": iid, "agent": agent, "now": now, "ttl_ms": ttl_ms}))


def release_lock(g: TenantGraph, iid: str, agent: str) -> None:
    g.write("MATCH (i:Incident {id: $iid}) WHERE i.lock_owner = $agent "
            "REMOVE i.lock_owner, i.lock_until", {"iid": iid, "agent": agent})


def resumable_incidents(g: TenantGraph, now: int) -> list[str]:
    """On boot: active incidents whose lock is free or expired. They continue from their last completed Action."""
    rows = g.read("MATCH (i:Incident) WHERE i.status IN $active AND (i.lock_owner IS NULL OR i.lock_until < $now) "
                  "RETURN i.id ORDER BY i.opened_at", {"active": list(ACTIVE_STATUSES), "now": now})
    return [r[0] for r in rows]


# ---- reads for agent context and the UI --------------------------------------------------------------------------
def incident_view(g: TenantGraph, iid: str) -> dict | None:
    """The incident subgraph as plain data: the agents' blackboard and the body of GET /incidents/{id}."""
    head = g.rows("MATCH (i:Incident {id: $iid}) RETURN i.id AS id, i.title AS title, i.severity AS severity, "
                  "i.status AS status, i.opened_at AS opened_at, i.resolved_at AS resolved_at, "
                  "i.steps AS steps, i.tokens AS tokens, i.status_reason AS reason, i.cost_usd AS cost_usd",
                  {"iid": iid})
    if not head:
        return None
    p = {"iid": iid}
    return {
        **head[0],
        "services": g.rows("MATCH (:Incident {id: $iid})-[r:AFFECTS]->(s:Service) "
                           "RETURN s.name AS name, r.role AS role, s.tier AS tier", p),
        "symptoms": g.rows("MATCH (:Incident {id: $iid})-[:SHOWED]->(y:Symptom)-[:OBSERVED_ON]->(t) "
                           "RETURN t.name AS service, y.kind AS kind, y.metric AS metric, y.value AS value "
                           "ORDER BY service, kind", p),
        "actions": g.rows("MATCH (:Incident {id: $iid})-[t:TOOK]->(a:Action) "
                          "RETURN a.id AS id, t.seq AS seq, a.agent AS agent, a.tool AS tool, a.args_json AS args, "
                          "a.outcome AS outcome, a.result_summary AS summary, a.rationale AS rationale "
                          "ORDER BY t.seq", p),
        "hypotheses": g.rows("MATCH (h:Hypothesis {incident_id: $iid}) OPTIONAL MATCH (h)-[:POINTS_TO]->(t) "
                             "RETURN h.id AS id, h.text AS text, h.score AS score, h.status AS status, "
                             "labels(t)[0] AS target_label, t.id AS target_id ORDER BY h.score DESC", p),
        "root_causes": g.rows("MATCH (:Incident {id: $iid})-[r:ROOT_CAUSE]->(n) "
                              "RETURN labels(n)[0] AS label, n.id AS id, r.confirmed_by AS confirmed_by", p),
        "handoffs": handoffs(g, iid),
    }
