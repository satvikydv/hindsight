"""Benchmark runner (PRD §12): the same incidents, in order, under three memory modes.

  off    no recall, no playbooks (a fresh agent every time)
  vector text embeddings of past postmortems only
  graph  structural recall + vector rerank + playbooks (Hindsight)

Each (mode, seed) gets its own tenant graph, so memory starts empty and modes never share anything. Incidents run in the
scenario order; the first of each fault class is "novel", later ones "repeat". Every incident is scored against the
simulator's ground truth, which agents never see.

Cost safety: a real-API run needs `--yes-spend` (the CLI prints an estimate and refuses otherwise), and `--max-cost`
stops the whole run, cleanly, once the dollar total passes the cap. The default LLM is the scripted expert policy, which
is free and validates the pipeline; only the real model can say whether graph memory beats vector memory.
"""

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from api import config
from api.agents.coordinator import Coordinator
from api.bench import metrics
from api.graph import client
from api.graph.client import TenantGraph
from api.llm import PRICES, LLM, Embedder
from api.sim.generator import build_company, seed_tenant
from api.sim.scenarios import build_scenarios
from api.sim.world import World
from api.tenancy import create_tenant, delete_tenant

DEFAULT_OUT = Path(__file__).resolve().parents[2] / "docs" / "results"
HOUR_MS = 3_600_000
# Measured live on the real models for easy incidents (see implementation-tracking.md). Harder fault classes cost more,
# hence the margin: this is a planning figure, not a promise.
COLD_COST, FAST_COST, MARGIN = 0.020, 0.008, 1.5


@dataclass
class BenchConfig:
    modes: tuple = metrics.MODES
    seeds: int = 3
    sets: tuple = (metrics.MAIN, metrics.HELDOUT)
    limit: int | None = None            # incidents per set (a cheap pilot)
    spacing_ms: int = HOUR_MS           # simulated time between incidents (decay is negligible at 1 h)
    workers: int = 4                    # (mode, seed) combinations run in parallel; each has its own tenant
    max_cost_usd: float | None = None
    company_seed: int = 42
    llm_name: str = "scripted"
    keep_tenants: bool = False


@dataclass
class IncidentRecord:
    mode: str
    seed: int
    set: str
    index: int
    scenario_id: str
    fault_class: str
    service: str
    novelty: str
    planted_injection: bool
    self_heal: bool
    incident_id: str
    status: str
    reason: str
    resolved: bool
    steps: int
    tokens: int
    cost_usd: float
    harmful: int
    fast_path: bool
    declined: bool
    confirmed: bool
    cause_correct: bool | None
    recalled_same_class: bool | None
    handoffs: int
    wall_ms: int


@dataclass
class Spend:
    cap: float | None
    total: float = 0.0
    stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, usd: float) -> None:
        with self._lock:
            self.total += usd or 0.0
            if self.cap is not None and self.total >= self.cap:
                self.stop.set()


# ---- scoring -------------------------------------------------------------------------------------------------------
def _cause_key(label: str, node_id=None, name=None, key=None) -> tuple:
    return (label, node_id) if label == "Deploy" else (label, key) if label == "ConfigFlag" else (label, name)


def confirmed_causes(g: TenantGraph, iid: str) -> set[tuple]:
    rows = g.rows("MATCH (:Incident {id: $i})-[:ROOT_CAUSE]->(n) RETURN labels(n)[0] AS label, n.id AS id, "
                  "n.name AS name, n.key AS key", {"i": iid})
    return {_cause_key(r["label"], r["id"], r["name"], r["key"]) for r in rows}


def truth_causes(truth: dict) -> set[tuple]:
    return {_cause_key(rc["label"], rc.get("id"), rc.get("name"), rc.get("key")) for rc in truth["root_cause"]}


def score(g: TenantGraph, world: World, mode: str, seed: int, set_name: str, index: int, sc: dict, res,
          past: dict[str, tuple[str, bool]], wall_ms: int) -> IncidentRecord:
    """past: incident id -> (fault_class, resolved) for incidents already run in this tenant."""
    iid = res.incident_id
    confirmed, truth = confirmed_causes(g, iid), truth_causes(world.truth(iid))
    recalled = None
    if mode != "off":
        eligible = any(cls == sc["fault_class"] and ok for cls, ok in past.values())
        if eligible:
            hits = {r[0] for r in g.read("MATCH (:Incident {id: $i})-[:SIMILAR_TO]->(p:Incident) RETURN p.id", {"i": iid})}
            recalled = any(past.get(h, ("", False))[0] == sc["fault_class"] for h in hits)
    declined = bool(g.read("MATCH (i:Incident {id: $i}) RETURN i.fast_path_declined", {"i": iid})[0][0])
    return IncidentRecord(
        mode=mode, seed=seed, set=set_name, index=index, scenario_id=sc["id"], fault_class=sc["fault_class"],
        service=sc["service"], novelty=sc.get("novelty") or ("heldout" if set_name == metrics.HELDOUT else set_name),
        planted_injection=bool(sc.get("planted_injection")), self_heal=bool(sc.get("self_heal")), incident_id=iid,
        status=res.status, reason=res.reason, resolved=res.status == "resolved", steps=res.steps, tokens=res.tokens,
        cost_usd=res.cost_usd or 0.0, harmful=res.harmful_actions, fast_path=res.fast_path, declined=declined,
        confirmed=bool(confirmed), cause_correct=(confirmed == truth) if confirmed else None,
        recalled_same_class=recalled, handoffs=res.handoffs, wall_ms=wall_ms)


# ---- running -------------------------------------------------------------------------------------------------------
def run_combo(mode: str, seed: int, cfg: BenchConfig, llm: LLM, embedder: Embedder, spend: Spend,
              progress=None) -> list[dict]:
    """One (mode, seed): a fresh tenant, every scenario in order. Always cleans up its tenant."""
    tid, _ = create_tenant(f"bench {mode} {seed} {int(time.time() * 1000) % 100000}", cfg.company_seed)
    g = TenantGraph(tid)
    records: list[dict] = []
    try:
        company = seed_tenant(g, cfg.company_seed)
        world = World(company, seed)
        sets = build_scenarios(company, cfg.company_seed)
        past: dict[str, tuple[str, bool]] = {}
        for set_name in cfg.sets:
            for index, sc in enumerate(sets[set_name][:cfg.limit], 1):
                if spend.stop.is_set():
                    return records
                co = Coordinator(g, world, llm, embedder, mode)
                t0 = time.monotonic()
                try:
                    iid = co.launch(sc["fault_class"], sc["service"], planted_injection=sc.get("planted_injection", False),
                                    self_heal=sc.get("self_heal", False))
                    res = co.run(iid)
                    rec = score(g, world, mode, seed, set_name, index, sc, res, past, int((time.monotonic() - t0) * 1000))
                    past[iid] = (sc["fault_class"], rec.resolved)
                    spend.add(rec.cost_usd)
                    records.append(rec.__dict__)
                except Exception as e:      # one broken incident must not sink the run; it is recorded as an error
                    records.append(_error_record(mode, seed, set_name, index, sc, e))
                if progress:
                    progress(mode, seed, set_name, index, records[-1], spend.total)
                world.advance(cfg.spacing_ms)
        return records
    finally:
        if not cfg.keep_tenants:
            try:
                delete_tenant(tid)
            except Exception:
                pass


def _error_record(mode, seed, set_name, index, sc, e) -> dict:
    return IncidentRecord(mode, seed, set_name, index, sc["id"], sc["fault_class"], sc["service"],
                          sc.get("novelty") or set_name, bool(sc.get("planted_injection")), bool(sc.get("self_heal")),
                          "", "error", f"{type(e).__name__}: {str(e)[:120]}", False, 0, 0, 0.0, 0, False, False, False,
                          None, None, 0, 0).__dict__


def run_benchmark(cfg: BenchConfig, llm_factory, embedder_factory, progress=None) -> dict:
    """Run every (mode, seed) and return the full result (records + aggregate + curves)."""
    embedder = embedder_factory()
    config.EMBED_DIM = embedder.dim            # the vector index must match the embedder in use
    spend = Spend(cfg.max_cost_usd)
    combos = [(m, s) for m in cfg.modes for s in range(1, cfg.seeds + 1)]
    started = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=max(1, min(cfg.workers, len(combos)))) as pool:
        futures = [pool.submit(run_combo, m, s, cfg, llm_factory(), embedder_factory(), spend, progress)
                   for m, s in combos]
        records = [r for f in futures for r in f.result()]
    partial = spend.stop.is_set()
    result = {
        "bench_id": started.strftime("%Y%m%d-%H%M%S"), "started_at": started.isoformat(), "partial": partial,
        "stopped_reason": f"cost cap ${cfg.max_cost_usd} reached" if partial else None,
        "config": {"llm": cfg.llm_name, "modes": list(cfg.modes), "seeds": cfg.seeds, "sets": list(cfg.sets),
                   "limit": cfg.limit, "spacing_ms": cfg.spacing_ms, "company_seed": cfg.company_seed,
                   "max_cost_usd": cfg.max_cost_usd, "models": {"strong": config.MODEL_STRONG,
                                                                  "fast": config.MODEL_FAST} if cfg.llm_name != "scripted" else None,
                   "prices_per_1m": {m: dict(zip(("input", "cached_input", "output"), p)) for m, p in PRICES.items()}},
        "records": records, "total_cost_usd": round(sum(r["cost_usd"] for r in records), 6),
    }
    result["aggregate"] = metrics.aggregate(records)
    result["curves"] = metrics.curves(records)
    return result


# ---- cost estimate -------------------------------------------------------------------------------------------------
def estimate_cost(cfg: BenchConfig) -> dict:
    """A planning range in dollars for a real-model run. Not a promise: harder fault classes cost more."""
    company = build_company(cfg.company_seed)
    sets = build_scenarios(company, cfg.company_seed)
    n = sum(len(sets[s][:cfg.limit]) for s in cfg.sets)
    novel = min(n, 8 * len(cfg.sets))                       # first of each class (roughly) is investigated cold
    per_seed = {"off": n * COLD_COST, "vector": n * COLD_COST, "graph": novel * COLD_COST + (n - novel) * FAST_COST}
    base = sum(per_seed[m] for m in cfg.modes) * cfg.seeds
    return {"incidents_per_mode_per_seed": n, "runs": len(cfg.modes) * cfg.seeds,
            "incidents_total": n * len(cfg.modes) * cfg.seeds, "low_usd": round(base, 2),
            "high_usd": round(base * MARGIN, 2)}


# ---- rule 3 proof --------------------------------------------------------------------------------------------------
def prove_graph_is_required() -> dict:
    """Hackathon rule 3: FalkorDB must be central, not decoration. Point the client at a port nothing listens on and try
    to run an incident: it has to fail. (Nothing to clean up: the real connection is restored afterwards.)"""
    from falkordb import FalkorDB

    saved = client._db
    try:
        try:
            client._db = FalkorDB(host="127.0.0.1", port=1)  # nothing listens on port 1; the client may refuse right here
            world = World(build_company(42), 1)
            Coordinator(TenantGraph("rule3_probe"), world, None, None, "off").launch("bad_deploy", "checkout-api")
            return {"graph_reachable": client.ping(), "incident_failed": False, "error": None}
        except Exception as e:
            return {"graph_reachable": False, "incident_failed": True, "error": f"{type(e).__name__}: {e}"[:160]}
    finally:
        client._db = saved


# ---- CLI -----------------------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hindsight benchmark: off / vector / graph memory")
    ap.add_argument("--llm", choices=["scripted", "openai"], default="scripted",
                    help="scripted = free expert policy (pipeline check); openai = real models (costs money)")
    ap.add_argument("--modes", nargs="+", choices=metrics.MODES, default=list(metrics.MODES))
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--sets", nargs="+", choices=["story", metrics.MAIN, metrics.HELDOUT],
                    default=[metrics.MAIN, metrics.HELDOUT])
    ap.add_argument("--limit", type=int, default=None, help="incidents per set (a cheap pilot)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-cost", type=float, default=None, help="stop cleanly once the dollar total passes this")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--estimate", action="store_true", help="print a cost estimate and exit")
    ap.add_argument("--yes-spend", action="store_true", help="required for --llm openai: confirms the cost was approved")
    ap.add_argument("--prove-rule3", action="store_true", help="show that the system fails without FalkorDB")
    a = ap.parse_args(argv)

    if a.prove_rule3:
        r = prove_graph_is_required()
        print(f"graph reachable: {r['graph_reachable']} | incident failed: {r['incident_failed']} | {r['error']}")
        return 0 if r["incident_failed"] else 1

    cfg = BenchConfig(modes=tuple(a.modes), seeds=a.seeds, sets=tuple(a.sets), limit=a.limit, workers=a.workers,
                      max_cost_usd=a.max_cost, llm_name=a.llm)
    est = estimate_cost(cfg)
    if a.llm == "openai":
        print(f"Real-model run: {est['incidents_total']} incidents over {est['runs']} runs, estimated "
              f"${est['low_usd']}-${est['high_usd']} (cap: {a.max_cost if a.max_cost else 'none'}).")
        if a.estimate or not a.yes_spend:
            print("Not running: pass --yes-spend once the cost has been approved." if not a.estimate else "")
            return 0 if a.estimate else 2
    elif a.estimate:
        print("Scripted run: free.")
        return 0

    if a.llm == "openai":
        from api.llm import OpenAIEmbedder, OpenAILLM
        llm_factory, embedder_factory = (lambda: OpenAILLM(cache=True)), OpenAIEmbedder
    else:
        from api.bench.scripted import expert_llm
        from api.llm import FakeEmbedder
        llm_factory, embedder_factory = expert_llm, (lambda: FakeEmbedder(64))

    lock = threading.Lock()

    def progress(mode, seed, set_name, index, rec, spent):
        with lock:
            print(f"[{mode:<6} seed {seed}] {set_name} {index:>2} {rec['fault_class']:<19} {rec['status']:<8} "
                  f"steps={rec['steps']:<2} fast={int(rec['fast_path'])} harm={rec['harmful']} spent=${spent:.3f}")

    result = run_benchmark(cfg, llm_factory, embedder_factory, progress)
    j, m = metrics.save(result, a.out)
    print(f"\nsaved {j}\n      {m}\n")
    print(metrics.render_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
