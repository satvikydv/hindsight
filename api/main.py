from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from api import config
from api.graph import client
from api.sim.generator import seed_tenant
from api.tenancy import TenantContext, create_tenant, tenant_context

app = FastAPI(title="Hindsight")


class NewTenant(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    seed: int = 42


@app.get("/health")
def health():
    up = client.ping()
    body = {"falkordb": "up" if up else "down", "llm_key_present": bool(config.LLM_API_KEY), "version": "0.1.0"}
    if not up:
        raise HTTPException(status_code=503, detail=body)  # the graph is required; no silent fallback
    return body


@app.post("/tenants")
def post_tenant(body: NewTenant):
    tenant_id, key = create_tenant(body.name)
    seed_tenant(client.TenantGraph(tenant_id), body.seed)
    return {"tenant_id": tenant_id, "api_key": key}


@app.get("/tenant")
def get_tenant(ctx: TenantContext = Depends(tenant_context)):
    """Who am I, and what is in my graph. Counts come from the graph bound to the caller's key."""
    nodes = ctx.graph.read("MATCH (n) RETURN count(n)")[0][0]
    services = ctx.graph.read("MATCH (s:Service) RETURN count(s)")[0][0]
    return {"tenant_id": ctx.tenant_id, "nodes": nodes, "services": services}
