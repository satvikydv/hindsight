"""Server-side runtime: per-tenant simulator, worker threads for incidents, and the live event log.

- The Coordinator blocks (LLM and graph calls), so each incident runs in a worker thread.
- One incident at a time per tenant: they share one simulator clock, so serialising them keeps results deterministic.
  Different tenants run in parallel.
- Every incident has an append-only event log that the SSE route replays from any point (Last-Event-ID). After a
  restart the log is gone, so the route rebuilds the timeline from the graph instead ("events are stored").
- The simulator's ground truth is snapshotted to disk after every step so a restart can resume active incidents.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from api.agents.coordinator import Coordinator, IncidentResult
from api.clock import wall_ms
from api.graph.client import TenantGraph
from api.llm import LLM, Embedder
from api.memory import episodic as ep
from api.memory import trace
from api.sim import store
from api.sim.faults import CATALOG, candidates
from api.sim.generator import Company, build_company
from api.sim.world import World
from api.tenancy import list_tenants, tenant_seed

log = logging.getLogger(__name__)

MAX_EVENT_JSON = 1500
TERMINAL = ("resolved", "failed", "error")


class NotConfigured(RuntimeError):
    """The LLM is not set up (no API key). Incidents cannot run; the rest of the API still works."""


@dataclass
class IncidentRun:
    iid: str
    tenant_id: str
    mode: str
    spec: dict
    status: str = "queued"                      # queued | running | resolved | failed | error
    coordinator: Coordinator | None = None
    result: IncidentResult | None = None
    events: list = field(default_factory=list)  # (id, event, data)
    done: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, event: str, data: dict) -> None:
        with self._lock:
            self.events.append((len(self.events) + 1, event, data))

    def since(self, last_id: int) -> list:
        with self._lock:
            return self.events[last_id:]


@dataclass
class TenantRuntime:
    tenant_id: str
    graph: TenantGraph
    company: Company
    world: World
    lock: threading.Lock = field(default_factory=threading.Lock)


def validate_spec(company: Company, spec: dict) -> None:
    """Reject a bad fault request up front, with a message the caller can act on."""
    fc, svc = spec.get("fault_class"), spec.get("service")
    if fc not in CATALOG:
        raise ValueError(f"unknown fault_class {fc!r}; one of {sorted(CATALOG)}")
    if svc is not None and svc not in candidates(company, fc):
        raise ValueError(f"{svc!r} is not a valid target for {fc}; valid: {candidates(company, fc)}")


class Runtime:
    def __init__(self, llm_factory: Callable[[], LLM], embedder_factory: Callable[[], Embedder], max_workers: int = 8):
        self._llm_factory, self._embedder_factory = llm_factory, embedder_factory
        self._llm: LLM | None = None
        self._embedder: Embedder | None = None
        self._tenants: dict[str, TenantRuntime] = {}
        self.runs: dict[str, IncidentRun] = {}
        self._guard = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="incident")

    # ---- dependencies ----------------------------------------------------------------------------------------------
    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = self._llm_factory()
        return self._llm

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = self._embedder_factory()
        return self._embedder

    def require_llm(self) -> None:
        self.llm  # raises NotConfigured if the factory cannot build one

    # ---- tenants ---------------------------------------------------------------------------------------------------
    def tenant(self, tenant_id: str) -> TenantRuntime:
        with self._guard:
            tr = self._tenants.get(tenant_id)
            if tr is None:
                company = build_company(tenant_seed(tenant_id))
                world = store.load(tenant_id, company) or World(company, company.seed)
                tr = self._tenants[tenant_id] = TenantRuntime(tenant_id, TenantGraph(tenant_id), company, world)
            return tr

    def forget(self, tenant_id: str) -> None:
        """Drop all in-memory and on-disk simulator state for a tenant (demo reset, tenant deletion)."""
        for run in list(self.runs.values()):
            if run.tenant_id == tenant_id and run.coordinator:
                run.coordinator.cancel(run.iid)
        with self._guard:
            self._tenants.pop(tenant_id, None)
            for iid in [i for i, r in self.runs.items() if r.tenant_id == tenant_id]:
                del self.runs[iid]
        store.delete(tenant_id)

    def save_world(self, tr: TenantRuntime) -> None:
        store.save(tr.tenant_id, tr.world)

    # ---- incidents -------------------------------------------------------------------------------------------------
    def submit(self, tenant_id: str, iid: str, spec: dict, mode: str = "graph") -> IncidentRun:
        run = IncidentRun(iid, tenant_id, mode, spec)
        self.runs[iid] = run
        self.pool.submit(self._work, run, False)
        return run

    def get(self, tenant_id: str, iid: str) -> IncidentRun | None:
        run = self.runs.get(iid)
        return run if run and run.tenant_id == tenant_id else None

    def cancel(self, tenant_id: str, iid: str) -> bool:
        run = self.get(tenant_id, iid)
        if not run or run.status in TERMINAL:
            return False
        if run.coordinator:
            run.coordinator.cancel(iid)
        else:                                   # still queued: it will see the flag the moment it starts
            run.spec["_cancel"] = True
        return True

    def _emit(self, run: IncidentRun, tr: TenantRuntime, event: str, data: dict) -> None:
        body = json.dumps(data, default=str)
        if len(body) > MAX_EVENT_JSON and "result" in data:
            data = {**data, "result": {"truncated": True, "preview": json.dumps(data["result"], default=str)[:600]}}
        run.push(event, data)
        if event in ("tool_result", "handoff", "agent_started"):
            self.save_world(tr)

    def _work(self, run: IncidentRun, resume: bool) -> None:
        tr = self.tenant(run.tenant_id)
        with tr.lock:                           # one incident at a time per tenant (shared simulator clock)
            run.status = "running"
            try:
                co = Coordinator(tr.graph, tr.world, self.llm, self.embedder, run.mode,
                                 emit=lambda e, d: self._emit(run, tr, e, d))
                run.coordinator = co
                if not resume:
                    s = run.spec
                    co.launch(s["fault_class"], s.get("service"), iid=run.iid,
                              planted_injection=bool(s.get("planted_injection")), self_heal=bool(s.get("self_heal")))
                    tr.graph.write("MATCH (i:Incident {id: $i}) SET i.mode = $m", {"i": run.iid, "m": run.mode})
                    self.save_world(tr)
                    if s.get("_cancel"):
                        co.cancel(run.iid)
                res = co.run(run.iid)
                run.result, run.status = res, res.status
            except Exception:
                log.exception("incident %s crashed", run.iid)
                run.status = "error"
                run.push("failed", {"incident": run.iid, "reason": "internal_error"})
            finally:
                self.save_world(tr)
                run.push("stream_end", {"incident": run.iid, "status": run.status})
                run.done.set()

    # ---- boot ------------------------------------------------------------------------------------------------------
    def resume_all(self, tenants: list[str] | None = None) -> list[str]:
        """Continue active incidents whose lock is free or expired (PRD §13). An incident whose simulator state was lost
        cannot be continued honestly (it would look healed), so it is failed with a clear reason instead."""
        resumed = []
        for tid in (tenants if tenants is not None else list_tenants()):
            try:
                tr = self.tenant(tid)
                for iid in ep.resumable_incidents(tr.graph, wall_ms()):
                    if iid not in tr.world.faults:
                        ep.set_status(tr.graph, iid, "failed", tr.world.now_ms, "simulator_state_lost")
                        continue
                    mode = (tr.graph.read("MATCH (i:Incident {id: $i}) RETURN i.mode", {"i": iid})[0][0]) or "graph"
                    run = IncidentRun(iid, tid, mode, {})
                    self.runs[iid] = run
                    self.pool.submit(self._work, run, True)
                    resumed.append(iid)
            except Exception:
                log.exception("could not resume incidents for tenant %s", tid)
        return resumed

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def backfill_events(g: TenantGraph, iid: str) -> list[tuple[str, dict]]:
    """Rebuild a stream from the graph for an incident whose in-memory log is gone (after a restart). The result
    payloads are the stored one-line summaries, not the original tool output."""
    tr = trace.incident_trace(g, iid)
    if tr is None:
        return []
    out: list[tuple[str, dict]] = []
    for e in tr["timeline"]:
        if e["type"] == "handoff":
            out.append(("handoff", {"incident": iid, "from": e["from"], "to": e["to"], "summary": e["summary"]}))
        elif e["type"] == "action":
            who = e["acting_agent"]
            out.append(("tool_called", {"agent": who, "tool": e["tool"], "args": e["args"]}))
            out.append(("tool_result", {"agent": who, "tool": e["tool"], "outcome": e["outcome"],
                                        "result": {"summary": e["summary"]}, "action_id": e["id"]}))
    status = tr["incident"]["status"]
    if status in ("resolved", "failed"):
        out.append((status, {"incident": iid, "reason": tr["incident"]["reason"]}))
    out.append(("stream_end", {"incident": iid, "status": status}))
    return out
