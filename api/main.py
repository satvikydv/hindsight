"""Hindsight HTTP API (PRD §10): REST for commands, Server-Sent Events for the live agent stream.

Every route except /health and POST /tenants is bound to a tenant by the X-Tenant-Key header; the graph name is derived
server-side from the key and never taken from a request.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from redis.exceptions import ConnectionError as RedisConnectionError

from api import config
from api.graph import client
from api.ids import new_id
from api.llm import LLM, Embedder, OpenAIEmbedder, OpenAILLM
from api.memory import episodic as ep
from api.memory import procedural as pr
from api.memory import semantic as sem
from api.memory import trace
from api.ratelimit import RateLimiter
from api.runtime import NotConfigured, Runtime, TERMINAL, backfill_events, validate_spec
from api.sim.generator import seed_tenant
from api.sim.scenarios import build_scenarios
from api.tenancy import (TenantContext, create_tenant, delete_tenant, resolve_key, tenant_context,
                         tenant_context_sse)

log = logging.getLogger(__name__)
VERSION = "0.2.0"
DEFAULT_FAULT = {"fault_class": "bad_deploy", "service": "checkout-api"}   # the demo's first incident


class NewTenant(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    seed: int = 42


class NewIncident(BaseModel):
    fault_class: str | None = None
    service: str | None = None
    scenario_id: str | None = None                         # e.g. story-1, bench-07, heldout-3
    mode: Literal["off", "vector", "graph"] = "graph"      # memory ablation, handy for the live demo
    planted_injection: bool = False
    self_heal: bool = False


class FactCorrection(BaseModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: str = Field(min_length=1)
    reason: str = ""


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def sse(event: str, data: dict, eid: int) -> str:
    return f"id: {eid}\nevent: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def create_app(llm: LLM | None = None, embedder: Embedder | None = None, resume: bool = True,
               demo: bool = True) -> FastAPI:
    def llm_factory() -> LLM:
        if llm is not None:
            return llm
        if not config.LLM_API_KEY:
            raise NotConfigured("set LLM_API_KEY in .env to run incidents")
        return OpenAILLM(cache=bool(os.getenv("LLM_CACHE")))

    def embedder_factory() -> Embedder:
        if embedder is not None:
            return embedder
        if not config.LLM_API_KEY:
            raise NotConfigured("set LLM_API_KEY in .env to run incidents")
        return OpenAIEmbedder()

    runtime = Runtime(llm_factory, embedder_factory)
    incident_limiter = RateLimiter(config.RATE_LIMIT_PER_HOUR)
    tenant_limiter = RateLimiter(config.RATE_LIMIT_PER_HOUR)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if demo and config.DEMO_TENANT_KEY and resolve_key(config.DEMO_TENANT_KEY) is None:
            tid, _ = create_tenant("demo", 42, config.DEMO_TENANT_KEY)
            seed_tenant(client.TenantGraph(tid), 42)
            log.info("seeded demo tenant %s", tid)
        if resume and client.ping():
            try:
                runtime.require_llm()
                log.info("resumed incidents: %s", runtime.resume_all())
            except NotConfigured:
                log.warning("LLM not configured: active incidents were not resumed")
        yield
        runtime.shutdown()

    app = FastAPI(title="Hindsight", version=VERSION, lifespan=lifespan)
    app.state.runtime = runtime
    origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                       allow_headers=["X-Tenant-Key", "Content-Type", "Last-Event-ID"])

    @app.exception_handler(RedisConnectionError)
    async def graph_down(request: Request, exc: RedisConnectionError):
        return JSONResponse(status_code=503, content={"detail": "graph database unavailable"})

    # ---- service ---------------------------------------------------------------------------------------------------
    @app.get("/health")
    def health():
        up = client.ping()
        body = {"falkordb": "up" if up else "down", "llm_key_present": bool(config.LLM_API_KEY), "version": VERSION,
                "demo_tenant": bool(config.DEMO_TENANT_KEY)}
        if not up:
            raise HTTPException(status_code=503, detail=body)   # the graph is required: no silent fallback
        return body

    @app.post("/tenants")
    def post_tenant(body: NewTenant, request: Request):
        if not tenant_limiter.allow(client_ip(request)):
            raise HTTPException(429, "too many tenants created from this address",
                                headers={"Retry-After": str(tenant_limiter.retry_after(client_ip(request)))})
        tenant_id, key = create_tenant(body.name, body.seed)
        seed_tenant(client.TenantGraph(tenant_id), body.seed)
        return {"tenant_id": tenant_id, "api_key": key}

    @app.get("/tenant")
    def get_tenant(ctx: TenantContext = Depends(tenant_context)):
        """Who am I, and what is in my graph. Counts come from the graph bound to the caller's key."""
        return {"tenant_id": ctx.tenant_id,
                "nodes": ctx.graph.read("MATCH (n) RETURN count(n)")[0][0],
                "services": ctx.graph.read("MATCH (s:Service) RETURN count(s)")[0][0]}

    @app.post("/demo/reset")
    def demo_reset(ctx: TenantContext = Depends(tenant_context)):
        """Reseed the public demo tenant (PRD §11). Only the demo key may call this."""
        if not config.DEMO_TENANT_KEY or ctx.tenant_id != resolve_key(config.DEMO_TENANT_KEY):
            raise HTTPException(403, "only the demo tenant can be reset")
        runtime.forget(ctx.tenant_id)
        delete_tenant(ctx.tenant_id)
        tid, _ = create_tenant("demo", 42, config.DEMO_TENANT_KEY)
        seed_tenant(client.TenantGraph(tid), 42)
        return {"tenant_id": tid, "reset": True}

    # ---- incidents -------------------------------------------------------------------------------------------------
    def resolve_spec(ctx: TenantContext, body: NewIncident) -> dict:
        tr = runtime.tenant(ctx.tenant_id)
        if body.scenario_id:
            sets = build_scenarios(tr.company, tr.company.seed)
            found = next((s for rows in sets.values() for s in rows if s["id"] == body.scenario_id), None)
            if not found:
                raise HTTPException(404, f"unknown scenario_id {body.scenario_id!r}")
            spec = {k: found[k] for k in ("fault_class", "service", "planted_injection", "self_heal")}
        elif body.fault_class or body.service:
            spec = {"fault_class": body.fault_class, "service": body.service,
                    "planted_injection": body.planted_injection, "self_heal": body.self_heal}
        else:
            spec = {**DEFAULT_FAULT, "planted_injection": body.planted_injection, "self_heal": body.self_heal}
        try:
            validate_spec(tr.company, spec)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return spec

    @app.post("/incidents", status_code=202)
    def post_incident(body: NewIncident, request: Request, ctx: TenantContext = Depends(tenant_context)):
        try:
            runtime.require_llm()
        except NotConfigured as e:
            raise HTTPException(503, f"LLM not configured: {e}")
        spec = resolve_spec(ctx, body)
        ip = client_ip(request)
        if not incident_limiter.allow(ip):
            raise HTTPException(429, f"rate limit: {config.RATE_LIMIT_PER_HOUR} incidents per hour per address",
                                headers={"Retry-After": str(incident_limiter.retry_after(ip))})
        iid = new_id()
        runtime.submit(ctx.tenant_id, iid, spec, body.mode)
        return {"incident_id": iid, "status": "queued", "fault_class": spec["fault_class"],
                "service": spec.get("service"), "mode": body.mode}

    @app.get("/incidents")
    def list_incidents(status: str | None = None, limit: int = Query(50, ge=1, le=200),
                       ctx: TenantContext = Depends(tenant_context)):
        where = "WHERE i.status = $status " if status else ""
        rows = ctx.graph.rows(
            f"MATCH (i:Incident) {where}"
            "RETURN i.id AS id, i.title AS title, i.severity AS severity, i.status AS status, i.status_reason AS reason, "
            "i.steps_to_resolve AS steps_to_resolve, i.tokens_to_resolve AS tokens_to_resolve, "
            "i.cost_to_resolve AS cost_to_resolve, i.steps AS steps, i.tokens AS tokens, i.opened_at AS opened_at, "
            "i.resolved_at AS resolved_at, i.fast_path_playbook IS NOT NULL AS fast_path, i.mode AS mode "
            "ORDER BY opened_at DESC LIMIT $limit", {"status": status, "limit": limit})
        known = {r["id"] for r in rows}
        pending = [{"id": r.iid, "title": r.spec.get("fault_class"), "status": r.status, "mode": r.mode}
                   for r in runtime.runs.values() if r.tenant_id == ctx.tenant_id and r.iid not in known
                   and r.status not in TERMINAL and (not status or r.status == status)]
        return {"incidents": pending + rows}

    def own_run(ctx: TenantContext, iid: str):
        return runtime.get(ctx.tenant_id, iid)

    @app.get("/incidents/{iid}")
    def get_incident(iid: str, ctx: TenantContext = Depends(tenant_context)):
        view = ep.incident_view(ctx.graph, iid)
        if view is None:
            run = own_run(ctx, iid)
            if run is None:
                raise HTTPException(404, "no such incident")
            return {"id": iid, "status": run.status, "mode": run.mode, "queued": True}
        run = own_run(ctx, iid)
        return {**view, "mode": run.mode if run else None, "running": bool(run and run.status in ("queued", "running"))}

    @app.get("/incidents/{iid}/trace")
    def get_trace(iid: str, format: Literal["json", "md"] = "json", ctx: TenantContext = Depends(tenant_context)):
        tr = trace.incident_trace(ctx.graph, iid)
        if tr is None:
            raise HTTPException(404, "no such incident")
        return PlainTextResponse(trace.render_markdown(tr), media_type="text/markdown") if format == "md" else tr

    @app.get("/incidents/{iid}/stream")
    async def stream_incident(iid: str, request: Request, ctx: TenantContext = Depends(tenant_context_sse),
                              last_event_id: str | None = Header(default=None)):
        run = own_run(ctx, iid)
        if run is None and ep.incident_view(ctx.graph, iid) is None:
            raise HTTPException(404, "no such incident")
        try:
            sent = int(last_event_id or 0)
        except ValueError:
            sent = 0

        async def gen():
            nonlocal sent
            if run is None:                                       # in-memory log is gone: rebuild from the graph
                for n, (event, data) in enumerate(backfill_events(ctx.graph, iid), 1):
                    if n > sent:
                        yield sse(event, data, n)
                return
            idle = 0
            while True:
                for eid, event, data in run.since(sent):
                    yield sse(event, data, eid)
                    sent, idle = eid, 0
                if run.done.is_set() and not run.since(sent):
                    return
                if await request.is_disconnected():
                    return
                idle += 1
                if idle % 150 == 0:                               # ~15 s: keep proxies from closing the connection
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.1)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/incidents/{iid}/cancel", status_code=202)
    def cancel_incident(iid: str, ctx: TenantContext = Depends(tenant_context)):
        view = ep.incident_view(ctx.graph, iid)
        run = own_run(ctx, iid)
        if view is None and run is None:
            raise HTTPException(404, "no such incident")
        if (view and view["status"] in ("resolved", "failed")) or not runtime.cancel(ctx.tenant_id, iid):
            raise HTTPException(409, "incident is not running")
        return {"incident_id": iid, "status": "cancelling"}

    @app.get("/actions/{action_id}/explain")
    def explain(action_id: str, ctx: TenantContext = Depends(tenant_context)):
        ex = trace.explain_action(ctx.graph, action_id)
        if ex is None:
            raise HTTPException(404, "no such action")
        return ex

    # ---- knowledge -------------------------------------------------------------------------------------------------
    @app.get("/graph/subgraph")
    def subgraph(center: str, hops: int = Query(1, ge=1, le=2), as_of: int | None = None,
                 ctx: TenantContext = Depends(tenant_context)):
        """Nodes and edges around a node (by name or id) for the graph panel. Capped at 300 nodes, 2 hops; episodic
        bulk (messages, actions, symptoms) and agents are left out so the picture stays readable."""
        g = ctx.graph
        found = g.read("MATCH (c) WHERE c.id = $c OR c.name = $c RETURN c.id LIMIT 1", {"c": center})
        if not found:
            raise HTTPException(404, f"no node named or identified {center!r}")
        cid = found[0][0]
        skip = "NOT x:Message AND NOT x:Action AND NOT x:Symptom AND NOT x:Agent"
        rows = g.read(f"MATCH p = (c {{id: $cid}})-[*1..{hops}]-(n) WHERE all(x IN nodes(p) WHERE {skip}) "
                      "RETURN DISTINCT n.id LIMIT 301", {"cid": cid})
        ids = [cid] + [r[0] for r in rows if r[0] != cid]
        truncated = max(0, len(ids) - 300)
        ids = ids[:300]
        nodes = g.rows("MATCH (n) WHERE n.id IN $ids RETURN n.id AS id, labels(n)[0] AS label, "
                       "coalesce(n.name, n.key, n.version, n.title, n.text) AS name, n.tier AS tier", {"ids": ids})
        edges = g.rows("MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids "
                       "RETURN a.id AS `from`, b.id AS `to`, type(r) AS type, r.valid_from AS valid_from, "
                       "r.valid_to AS valid_to", {"ids": ids})
        if as_of is not None:    # what we believed at that time: keep only edges valid then
            edges = [e for e in edges if (e["valid_from"] is None or e["valid_from"] <= as_of)
                     and (e["valid_to"] is None or e["valid_to"] > as_of)]
        return {"center": cid, "nodes": nodes, "edges": edges, "truncated": truncated}

    @app.get("/playbooks")
    def playbooks(ctx: TenantContext = Depends(tenant_context)):
        tr = runtime.tenant(ctx.tenant_id)
        pr.apply_decay(ctx.graph, tr.world.now_ms)
        return {"playbooks": pr.list_playbooks(ctx.graph)}

    @app.post("/facts/correct")
    def correct_fact(body: FactCorrection, ctx: TenantContext = Depends(tenant_context)):
        tr = runtime.tenant(ctx.tenant_id)
        try:
            return sem.correct(ctx.graph, body.subject, body.predicate, body.value, body.reason, at=tr.world.now_ms)
        except sem.NotFound as e:
            return JSONResponse(status_code=404, content={"detail": str(e), "suggestions": e.suggestions})
        except ValueError as e:
            raise HTTPException(400, str(e))

    return app


app = create_app()
