"""Semantic memory: what is true about the system, versioned (PRD §5/§6 temporal facts).

Corrections never delete: the old fact/edge gets valid_to = now, the new one opens at now, and a SUPERSEDES edge links
them. Reads default to the current state; as-of reads answer "what did we believe at time t".
"""

import difflib

from api.graph import queries as q
from api.graph.client import TenantGraph
from api.ids import new_id

SUBJECT_LABELS = {"Service", "Datastore", "Team", "Person", "Deploy", "ConfigFlag"}


class NotFound(LookupError):
    """A subject or team that does not exist. `suggestions` are the closest names (API returns them with the 404)."""

    def __init__(self, what: str, name: str, suggestions: list[str]):
        super().__init__(f"{what} {name!r} not found")
        self.suggestions = suggestions


def _closest(name: str, options: list[str]) -> list[str]:
    return difflib.get_close_matches(name, options, n=3, cutoff=0.5)


def resolve_subject(g: TenantGraph, name: str) -> tuple[str, str]:
    """Service or Datastore by name -> (label, id). Raises NotFound with edit-distance suggestions."""
    for label in ("Service", "Datastore"):
        rows = g.read(f"MATCH (n:{label} {{name: $n}}) RETURN n.id", {"n": name})
        if rows:
            return label, rows[0][0]
    names = [r[0] for r in g.read("MATCH (n) WHERE n:Service OR n:Datastore RETURN n.name")]
    raise NotFound("subject", name, _closest(name, names))


def write_fact(g: TenantGraph, label: str, subject_id: str, predicate: str, value: str, text: str, source: str,
               at: int, reason: str = "", embedding: list[float] | None = None,
               derived_from: str | None = None, confidence: float = 0.7) -> dict:
    """Assert predicate=value about a subject. Returns {status, id}.

    created: first fact for (subject, predicate).  unchanged: same value already current.
    superseded: a different current value was closed and linked with SUPERSEDES.
    kept_human: the current fact came from a human and an agent tried to override it; the agent write is dropped.
    """
    if label not in SUBJECT_LABELS:
        raise ValueError(f"cannot write a fact about a {label!r}")
    cur = g.rows(f"MATCH (f:Fact {{predicate: $p}})-[:ABOUT]->(:{label} {{id: $sid}}) WHERE f.valid_to IS NULL "
                 "RETURN f.id AS id, f.value AS value, f.source AS source", {"p": predicate, "sid": subject_id})
    old = cur[0] if cur else None
    if old and old["value"] == value:
        return {"status": "unchanged", "id": old["id"]}
    if old and old["source"] == "human" and source != "human":
        return {"status": "kept_human", "id": old["id"]}

    fid = new_id()
    g.write(f"MATCH (n:{label} {{id: $sid}}) "
            "CREATE (f:Fact {id: $fid, text: $text, predicate: $p, value: $value, confidence: $conf, source: $src, "
            "valid_from: $at, created_at: $at}) CREATE (f)-[:ABOUT]->(n)",
            {"sid": subject_id, "fid": fid, "text": text[:300], "p": predicate, "value": value, "conf": confidence,
             "src": source, "at": at})
    if embedding is not None:
        g.write("MATCH (f:Fact {id: $fid}) SET f.embedding = vecf32($vec)", {"fid": fid, "vec": embedding})
    if derived_from:
        g.write("MATCH (f:Fact {id: $fid}) MATCH (i:Incident {id: $iid}) MERGE (f)-[:DERIVED_FROM]->(i)",
                {"fid": fid, "iid": derived_from})
    if old:
        g.write("MATCH (o:Fact {id: $oid}) SET o.valid_to = $at "
                "WITH o MATCH (n:Fact {id: $fid}) CREATE (n)-[:SUPERSEDES {at: $at, reason: $reason}]->(o)",
                {"oid": old["id"], "fid": fid, "at": at, "reason": reason[:200]})
        return {"status": "superseded", "id": fid, "superseded": old["id"]}
    return {"status": "created", "id": fid}


def facts_about(g: TenantGraph, node_id: str, as_of: int) -> list[dict]:
    """Q12: facts about a node as of time `as_of` (pass the current time for the present state)."""
    return g.rows(q.Q12_FACTS_AS_OF, {"node_id": node_id, "t": as_of})


def fact_history(g: TenantGraph, node_id: str, predicate: str) -> list[dict]:
    """Every value ever asserted for (node, predicate), oldest first, for the as-of slider."""
    return g.rows("MATCH (f:Fact {predicate: $p})-[:ABOUT]->(n {id: $nid}) "
                  "RETURN f.value AS value, f.source AS source, f.valid_from AS valid_from, f.valid_to AS valid_to "
                  "ORDER BY f.valid_from", {"p": predicate, "nid": node_id})


def current_owner(g: TenantGraph, service: str) -> str | None:
    rows = g.rows(q.Q8_OWNERS, {"svc": service})
    return rows[0]["team"] if rows else None


def owners_as_of(g: TenantGraph, service: str, t: int) -> list[str]:
    """Which team owned the service at time t."""
    return [r[0] for r in g.read(
        "MATCH (team:Team)-[o:OWNS]->(:Service {name: $svc}) "
        "WHERE o.valid_from <= $t AND (o.valid_to IS NULL OR o.valid_to > $t) RETURN team.name",
        {"svc": service, "t": t})]


def correct(g: TenantGraph, subject: str, predicate: str, value: str, reason: str, at: int,
            source: str = "human") -> dict:
    """POST /facts/correct. predicate 'owner' supersedes the OWNS edge (Q14); anything else becomes a Fact.
    Raises NotFound (with suggestions) for an unknown subject or team."""
    label, sid = resolve_subject(g, subject)
    if predicate == "owner":
        if label != "Service":
            raise ValueError("only a Service has an owner")
        teams = [r[0] for r in g.read("MATCH (t:Team) RETURN t.name")]
        if value not in teams:
            raise NotFound("team", value, _closest(value, teams))
        before = current_owner(g, subject)
        if before == value:
            return {"status": "unchanged", "owner": value}
        if before is None:
            g.write("MATCH (t:Team {name: $team}) MATCH (s:Service {id: $sid}) "
                    "CREATE (t)-[:OWNS {valid_from: $at, source: $src}]->(s)",
                    {"team": value, "sid": sid, "at": at, "src": source})
        else:
            g.write(q.Q14_SUPERSEDE_OWNERSHIP, {"svc": subject, "team": value, "now": at, "source": source})
        write_fact(g, label, sid, "owner", value, f"{subject} is now owned by Team {value} ({reason})",
                   source, at, reason, confidence=1.0 if source == "human" else 0.7)
        return {"status": "superseded" if before else "created", "owner": value, "previous": before}
    return write_fact(g, label, sid, predicate, value, f"{subject} {predicate}: {value}", source, at, reason,
                      confidence=1.0 if source == "human" else 0.7)
