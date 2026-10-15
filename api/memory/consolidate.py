"""Consolidation: what the Scribe turns a finished incident into (PRD §6).

Reinforce the playbook that was followed, merge or create a playbook from the fix that worked, write up to 3 lessons as
temporal facts, and record who worked on it. Deterministic and graph-only: it reads the Actions the crew wrote and
never sees the simulator's ground truth, so what it learns is what the crew could actually observe.
"""

import logging

from api.graph.client import TenantGraph
from api.llm import Embedder, EmbeddingUnavailable
from api.memory import episodic as ep
from api.memory import procedural as pr
from api.memory import semantic as sem

log = logging.getLogger(__name__)

ACT_TOOLS = {"rollback", "restart", "scale", "set_flag", "failover", "renew_cert"}
MAX_LESSONS = 3


def winning_sequence(view: dict) -> list[dict]:
    """The remediation actions that worked, in order. Wrong guesses (no_effect, harmful) and refusals are dropped."""
    import json

    return [{"tool": a["tool"], "args": json.loads(a["args"] or "{}")}
            for a in view["actions"] if a["tool"] in ACT_TOOLS and a["outcome"] == "success"]


def infer_fault_class(seq: list[dict], datastores: set[str]) -> str:
    """Name the fix pattern. Derived from what worked, since the crew is never told the ground-truth class."""
    tools = tuple(s["tool"] for s in seq)
    scales_datastore = bool(seq) and seq[0]["tool"] == "scale" and seq[0]["args"].get("target") in datastores
    if tools == ("rollback",):
        return "bad_deploy"
    if tools == ("set_flag",):
        return "bad_flag"
    if scales_datastore and tools == ("scale", "restart"):
        return "db_pool_exhaustion"
    if scales_datastore and tools == ("scale", "restart", "rollback"):
        return "cascading"
    if tools in (("restart",), ("restart", "rollback")):
        return "memory_leak"
    if tools == ("renew_cert",):
        return "cert_expiry"
    if tools == ("failover",):
        return "dependency_outage"
    if tools == ("scale",):
        return "traffic_spike"
    return "other:" + "+".join(tools)


def templatize(step: dict, origin: str, datastores: set[str]) -> dict:
    """Replace this incident's concrete names with placeholders so the playbook transfers to other services."""
    tool, args = step["tool"], step["args"]
    out = {}
    for k, v in args.items():
        if tool == "set_flag" and k == "key":
            out[k] = "$changed_flag"
        elif tool == "set_flag" and k == "value":
            out[k] = "$previous_value"
        elif v == origin:
            out[k] = "$origin"
        elif v in datastores:
            out[k] = "$datastore"
        else:
            out[k] = v
    return {"tool": tool, "args_template": out}


def _followed_playbook(g: TenantGraph, iid: str) -> str | None:
    rows = g.read("MATCH (i:Incident {id: $iid}) RETURN i.fast_path_playbook", {"iid": iid})
    if rows and rows[0][0]:
        return rows[0][0]
    rows = g.read("MATCH (:Incident {id: $iid})-[:TOOK]->(:Action {tool: 'playbook_steps'})-[:CITED]->(pb:Playbook) "
                  "RETURN pb.id LIMIT 1", {"iid": iid})
    return rows[0][0] if rows else None


def _lessons(g: TenantGraph, view: dict, fault_class: str, seq: list[dict], origin: str) -> list[dict]:
    """Up to 3 short facts: (label, node_id, predicate, value, text)."""
    sid = g.read("MATCH (s:Service {name: $n}) RETURN s.id", {"n": origin})[0][0]
    fix = " then ".join(f"{s['tool']}({', '.join(str(v) for v in s['args'].values())})" for s in seq)
    out = [{"label": "Service", "id": sid, "predicate": f"lesson:fix:{fault_class}", "value": fix,
            "text": f"{origin}: {fault_class} was resolved by {fix}"}]
    for rc in view["root_causes"]:
        if rc["label"] == "Deploy":
            r = g.rows("MATCH (d:Deploy {id: $id})-[:DEPLOYED]->(s:Service) OPTIONAL MATCH (p:Person)-[:AUTHORED]->(d) "
                       "RETURN s.name AS service, d.version AS version, p.name AS author", {"id": rc["id"]})
            if r:
                out.append({"label": "Service", "id": g.read("MATCH (s:Service {name: $n}) RETURN s.id",
                                                             {"n": r[0]["service"]})[0][0],
                            "predicate": f"lesson:bad_deploy:{rc['id']}", "value": r[0]["version"],
                            "text": f"{r[0]['service']} {r[0]['version']} (by {r[0]['author']}) broke production and was "
                                    f"rolled back"})
        elif rc["label"] == "Datastore":
            r = g.rows("MATCH (d:Datastore {id: $id}) RETURN d.name AS name", {"id": rc["id"]})
            if r:
                out.append({"label": "Datastore", "id": rc["id"], "predicate": "lesson:saturation",
                            "value": origin, "text": f"{r[0]['name']} saturates and hurts its callers, seen via {origin}"})
    return out[:MAX_LESSONS]


def consolidate(g: TenantGraph, embedder: Embedder | None, iid: str, at: int) -> dict:
    """Run after the incident ends (resolved or failed). Returns a report of what was learned."""
    view = ep.incident_view(g, iid)
    resolved = view["status"] == "resolved"
    origin = next(s["name"] for s in view["services"] if s["role"] == "origin")
    datastores = {r[0] for r in g.read("MATCH (d:Datastore) RETURN d.name")}
    seq = winning_sequence(view)
    report: dict = {"resolved": resolved, "fault_class": None, "playbook": None, "playbook_action": "none",
                    "lessons": 0, "followed_playbook": None}

    pr.apply_decay(g, at)
    followed = _followed_playbook(g, iid)
    report["followed_playbook"] = followed
    if followed:
        # "Followed" only counts if the Remediator actually ran one of the playbook's tools. If it checked the
        # precondition, saw it did not hold, and fixed the incident some other way (or took no action), the playbook was
        # never tried: neither a win nor a failure, so it is not reinforced or penalised.
        playbook_tools = {s["tool"] for s in pr.steps_of(g, followed)}
        tried = any(a["agent"] == "remediator" and a["tool"] in playbook_tools
                    and a["outcome"] in ("success", "no_effect", "harmful") for a in view["actions"])
        if not tried:
            report["playbook_note"] = "not tried: the Remediator never ran any of its steps"
            followed = None
    if resolved and not seq:
        report["note"] = "self_healed: nothing was done, so nothing is learned"
        return report

    fault_class = infer_fault_class(seq, datastores) if seq else None
    report["fault_class"] = fault_class

    if followed:
        want = [s["tool"] for s in pr.steps_of(g, followed)]
        if resolved and want == [s["tool"] for s in seq]:
            pr.reinforce(g, followed, True, at)
            pr.reinforce_steps(g, followed, {n: True for n in range(1, len(want) + 1)})
            pr.link_resolution(g, iid, followed)
            report.update(playbook=followed, playbook_action="reinforced")
            followed_ok = True
        else:  # it was tried and was not what worked (or the incident failed): count the failure
            pr.reinforce(g, followed, False, at)
            followed_ok = False
    else:
        followed_ok = False

    if resolved and seq and not followed_ok:
        steps = [templatize(s, origin, datastores) for s in seq]
        dup = pr.find_duplicate(g, fault_class, steps)
        if dup:
            pr.reinforce(g, dup, True, at)
            pr.reinforce_steps(g, dup, {n: True for n in range(1, len(steps) + 1)})
            pr.link_resolution(g, iid, dup)
            report.update(playbook=dup, playbook_action="merged")
        else:
            name = f"{fault_class}: " + " then ".join(s["tool"] for s in seq)
            report.update(playbook=pr.create_playbook(g, name, fault_class, steps, iid, at),
                          playbook_action="created")

    if resolved and seq:
        for lesson in _lessons(g, view, fault_class, seq, origin):
            vec = None
            if embedder:
                try:
                    (vec,) = embedder.embed([lesson["text"]])
                except EmbeddingUnavailable as e:
                    log.warning("lesson not embedded: %s", e)
            sem.write_fact(g, lesson["label"], lesson["id"], lesson["predicate"], lesson["value"], lesson["text"],
                           "agent", at, derived_from=iid, embedding=vec)
            report["lessons"] += 1
    return report


def link_people(g: TenantGraph, iid: str, at: int) -> dict:
    """WORKED_ON for the on-call responder (the owning team's SRE) and the author of a deploy that caused it."""
    view = ep.incident_view(g, iid)
    origin = next(s["name"] for s in view["services"] if s["role"] == "origin")
    linked = []
    sre = g.rows("MATCH (t:Team)-[o:OWNS]->(:Service {name: $svc}) WHERE o.valid_to IS NULL "
                 "MATCH (p:Person {role: 'SRE'})-[m:MEMBER_OF]->(t) WHERE m.valid_to IS NULL "
                 "RETURN p.id AS id, p.name AS name ORDER BY p.name LIMIT 1", {"svc": origin})
    pairs = [(r["id"], "responder", r["name"]) for r in sre]
    for rc in view["root_causes"]:
        if rc["label"] == "Deploy":
            pairs += [(r["id"], "author_of_cause", r["name"]) for r in g.rows(
                "MATCH (p:Person)-[:AUTHORED]->(:Deploy {id: $id}) RETURN p.id AS id, p.name AS name", {"id": rc["id"]})]
    for pid, role, name in pairs:
        g.write("MATCH (p:Person {id: $pid}) MATCH (i:Incident {id: $iid}) "
                "MERGE (p)-[w:WORKED_ON {role: $role}]->(i) SET w.at = $at", {"pid": pid, "iid": iid, "role": role,
                                                                              "at": at})
        linked.append({"person": name, "role": role})
    return {"linked": linked}
