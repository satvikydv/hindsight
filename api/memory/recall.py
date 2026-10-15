"""Recall at incident start: structure first, text second (PRD §6).

1. Structural: past resolved incidents with the same shape (Q4: symptom Jaccard, domain, tier) plus shared neighbours (Q17).
2. Text: vector search over incident signatures (Q5), top 10.
3. Rerank: 0.5 x structural + 0.3 x vector similarity + 0.2 x recency; keep 3 and write SIMILAR_TO {score, basis}.
4. Playbook candidates: active playbooks that resolved those incidents (Q6).

Modes drive the ablation (PRD §12): off = no recall, vector = text embeddings only, graph = everything above.
"""

import logging
from dataclasses import dataclass, field

from api.graph import queries as q
from api.graph.client import TenantGraph
from api.llm import Embedder, EmbeddingUnavailable
from api.memory import episodic as ep
from api.memory import procedural as pr

log = logging.getLogger(__name__)

W_STRUCT, W_VECTOR, W_RECENCY = 0.5, 0.3, 0.2
# structural overlap = weighted mix of these five signals (they sum to 1). Symptom kinds and triage-time signals say
# what KIND of failure it is, so they carry most of the weight; domain and tier barely matter (a bad-deploy playbook
# transfers across domains); shared dependencies add some.
S_JACCARD, S_SIGNALS, S_DOMAIN, S_TIER, S_NEIGHBOURS = 0.35, 0.35, 0.05, 0.05, 0.20
SIGNAL_WINDOW_MS = 6 * 3_600_000
TOP_K = 3
MODES = ("off", "vector", "graph")


def compute_signals(g: TenantGraph, iid: str, origin: str, now: int) -> list[str]:
    """Cause-discriminating facts the graph can answer at triage time, before anyone investigates: is there a recent
    (not reverted) deploy or flag change near the origin, a saturated datastore, a crashloop or an expired cert?
    Two incidents with the same signals are the same KIND of failure even when their alerts look alike (a bad deploy and
    a hard-down dependency both show error_rate, but only one has a recent deploy)."""
    since = now - SIGNAL_WINDOW_MS
    names = [r["name"] for r in g.rows(q.Q18_NEIGHBOURHOOD, {"svc": origin}) if r["label"] == "Service"]
    found: set[str] = set()
    if g.rows(q.Q2_RECENT_CHANGES, {"svc": origin, "since": since}):
        found.add("deploy")
    if g.rows(q.Q21_RECENT_FLAG_CHANGES, {"names": names, "since": since}):
        found.add("flag")
    for r in g.rows("MATCH (:Incident {id: $i})-[:SHOWED]->(y:Symptom)-[:OBSERVED_ON]->(n) "
                    "RETURN y.kind AS kind, n.name AS node, labels(n)[0] AS label", {"i": iid}):
        if r["kind"] in ("crashloop", "cert") and r["node"] == origin:
            found.add(r["kind"])
        if r["kind"] == "saturation" and r["label"] == "Datastore":
            found.add("saturation")
    return sorted(found)


@dataclass
class Recall:
    similar: list[dict] = field(default_factory=list)      # best first: incident, score, basis, parts, ...
    playbooks: list[dict] = field(default_factory=list)    # Q6 rows
    warnings: list[str] = field(default_factory=list)


def signature_text(view: dict) -> str:
    """What gets embedded: the title and the symptoms (kind on service). Service names stay in, as in a text-only RAG."""
    sym = ", ".join(f"{s['kind']} on {s['service']}" for s in view["symptoms"])
    return f"{view['title']}. symptoms: {sym}"


def index_incident(g: TenantGraph, embedder: Embedder, iid: str) -> bool:
    """Store the incident's signature embedding. False (no exception) if embeddings are unavailable."""
    view = ep.incident_view(g, iid)
    try:
        (vec,) = embedder.embed([signature_text(view)])
    except EmbeddingUnavailable as e:
        log.warning("embedding unavailable for %s: %s", iid, e)
        return False
    ep.set_signature(g, iid, vec)
    return True


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _recency(age_ms: int) -> float:
    return 0.5 ** (max(0, age_ms) / pr.HALF_LIFE_MS)


def _structural(g: TenantGraph, iid: str) -> dict[str, dict]:
    rows = g.rows(q.Q4_STRUCTURAL_RECALL, {"iid": iid})
    out: dict[str, dict] = {}
    for r in rows:  # a cascading incident has two ROOT_CAUSE edges and so two rows: keep the best, list both cause types
        cur = out.setdefault(r["incident"], {"jaccard": r["symptomJaccard"], "same_domain": bool(r["sameDomain"]),
                                             "same_tier": bool(r["sameTier"]), "cause_types": []})
        if r["causeType"]:
            cur["cause_types"].append(r["causeType"])
    if not out:
        return {}
    deps = {r["incident"]: set(r["deps"]) for r in g.rows(q.Q17_ORIGIN_DEPENDENCIES, {"ids": [iid, *out]})}
    signals = {r["id"]: set(r["signals"] or []) for r in g.rows(
        "MATCH (i:Incident) WHERE i.id IN $ids RETURN i.id AS id, i.signals AS signals", {"ids": [iid, *out]})}
    mine, my_signals = deps.get(iid, set()), signals.get(iid, set())
    for pid, s in out.items():
        s["neighbours"] = _jaccard(mine, deps.get(pid, set()))
        theirs = signals.get(pid, set())
        s["signals"] = 1.0 if not my_signals and not theirs else _jaccard(my_signals, theirs)   # both empty: same
        s["score"] = (S_JACCARD * s["jaccard"] + S_SIGNALS * s["signals"] + S_DOMAIN * s["same_domain"]
                      + S_TIER * s["same_tier"] + S_NEIGHBOURS * s["neighbours"])
    return out


def _vector(g: TenantGraph, embedder: Embedder, iid: str) -> dict[str, float]:
    """Cosine similarity per past resolved incident. FalkorDB's cosine `score` is a distance, so sim = 1 - score."""
    view = ep.incident_view(g, iid)
    (vec,) = embedder.embed([signature_text(view)])
    rows = g.rows(q.Q5_VECTOR_RECALL, {"vec": vec})
    ids = [r["incident"] for r in rows if r["incident"] != iid]
    if not ids:
        return {}
    resolved = {r[0] for r in g.read("MATCH (i:Incident) WHERE i.id IN $ids AND i.status = 'resolved' RETURN i.id",
                                     {"ids": ids})}
    return {r["incident"]: max(0.0, 1.0 - r["score"]) for r in rows if r["incident"] in resolved}


def recall_similar(g: TenantGraph, embedder: Embedder, iid: str, now: int, mode: str = "graph") -> Recall:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    out = Recall()
    if mode == "off":
        return out

    structural = _structural(g, iid) if mode == "graph" else {}
    vector: dict[str, float] = {}
    try:
        vector = _vector(g, embedder, iid)
    except EmbeddingUnavailable as e:
        if mode == "vector":
            raise  # vector-only recall has nothing to fall back to
        out.warnings.append(f"embeddings unavailable, structural recall only: {e}")
        log.warning("recall for %s fell back to structure only: %s", iid, e)

    ids = set(structural) | set(vector)
    if not ids:
        return out
    info = {r["id"]: r for r in g.rows(
        "MATCH (i:Incident) WHERE i.id IN $ids RETURN i.id AS id, i.title AS title, i.resolved_at AS resolved_at",
        {"ids": list(ids)})}

    ranked = []
    for pid in ids:
        s, v = structural.get(pid, {}).get("score", 0.0), vector.get(pid, 0.0)
        rec = _recency(now - (info[pid]["resolved_at"] or now))
        score = v if mode == "vector" else W_STRUCT * s + W_VECTOR * v + W_RECENCY * rec
        basis = "text" if mode == "vector" else ("both" if pid in structural and pid in vector else
                                                 "structure" if pid in structural else "text")
        ranked.append({"incident": pid, "title": info[pid]["title"], "score": round(score, 4), "basis": basis,
                       "parts": {"structural": round(s, 4), "vector": round(v, 4), "recency": round(rec, 4)},
                       "cause_types": structural.get(pid, {}).get("cause_types", [])})
    ranked.sort(key=lambda r: (-r["score"], r["incident"]))
    out.similar = ranked[:TOP_K]

    for r in out.similar:
        g.write("MATCH (a:Incident {id: $a}) MATCH (b:Incident {id: $b}) "
                "MERGE (a)-[s:SIMILAR_TO]->(b) SET s.score = $score, s.basis = $basis, s.structural = $structural",
                {"a": iid, "b": r["incident"], "score": r["score"], "basis": r["basis"],
                 "structural": r["parts"]["structural"]})
    if mode == "graph":
        out.playbooks = pr.candidates(g, [r["incident"] for r in out.similar])
    return out
