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
# structural overlap = weighted mix of these four signals (they sum to 1)
S_JACCARD, S_DOMAIN, S_TIER, S_NEIGHBOURS = 0.4, 0.2, 0.1, 0.3
TOP_K = 3
MODES = ("off", "vector", "graph")


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
    mine = deps.get(iid, set())
    for pid, s in out.items():
        s["neighbours"] = _jaccard(mine, deps.get(pid, set()))
        s["score"] = (S_JACCARD * s["jaccard"] + S_DOMAIN * s["same_domain"] + S_TIER * s["same_tier"]
                      + S_NEIGHBOURS * s["neighbours"])
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
                "MERGE (a)-[s:SIMILAR_TO]->(b) SET s.score = $score, s.basis = $basis",
                {"a": iid, "b": r["incident"], "score": r["score"], "basis": r["basis"]})
    if mode == "graph":
        out.playbooks = pr.candidates(g, [r["incident"] for r in out.similar])
    return out
