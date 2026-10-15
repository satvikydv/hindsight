"""API key -> tenant graph. The graph name is derived here, server-side, and nowhere else."""

import hashlib
import re
import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException, Query
from ulid import ULID

from api.graph.client import TenantGraph, get_db, graph_name
from api.graph.schema import init_graph

REGISTRY = "_registry"


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    graph: TenantGraph


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _registry():
    return get_db().select_graph(REGISTRY)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "tenant"


def create_tenant(name: str, seed: int = 42, key: str | None = None) -> tuple[str, str]:
    """Create an empty, indexed tenant graph. Returns (tenant_id, api_key); a generated key is shown once.
    `seed` is remembered so the simulator can rebuild the same company after a restart; `key` lets the demo tenant
    keep a fixed key from the environment."""
    tenant_id = f"{_slug(name)}_{str(ULID()).lower()[-6:]}"
    key = key or "hk_" + secrets.token_urlsafe(24)
    _registry().query(
        "CREATE (:Tenant {id: $id, name: $name, key_hash: $h, seed: $seed})",
        {"id": tenant_id, "name": name, "h": _hash(key), "seed": seed},
    )
    init_graph(TenantGraph(tenant_id))
    return tenant_id, key


def resolve_key(key: str | None) -> str | None:
    if not key:
        return None
    rows = _registry().ro_query(
        "MATCH (t:Tenant {key_hash: $h}) RETURN t.id", {"h": _hash(key)}
    ).result_set
    return rows[0][0] if rows else None


def tenant_seed(tenant_id: str) -> int:
    rows = _registry().ro_query("MATCH (t:Tenant {id: $id}) RETURN t.seed", {"id": tenant_id}).result_set
    return rows[0][0] if rows and rows[0][0] is not None else 42


def tenant_name(tenant_id: str) -> str | None:
    rows = _registry().ro_query("MATCH (t:Tenant {id: $id}) RETURN t.name", {"id": tenant_id}).result_set
    return rows[0][0] if rows else None


def list_tenants() -> list[str]:
    return [r[0] for r in _registry().ro_query("MATCH (t:Tenant) RETURN t.id ORDER BY t.id").result_set]


def delete_tenant(tenant_id: str) -> None:
    TenantGraph(tenant_id).delete()
    _registry().query("MATCH (t:Tenant {id: $id}) DELETE t", {"id": tenant_id})


def tenant_context(x_tenant_key: str | None = Header(default=None)) -> TenantContext:
    """FastAPI dependency. Unknown or missing key -> 401. No code path takes a graph name from a request."""
    tenant_id = resolve_key(x_tenant_key)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="unknown tenant key")
    return TenantContext(tenant_id=tenant_id, graph=TenantGraph(tenant_id))


def tenant_context_sse(x_tenant_key: str | None = Header(default=None),
                       tenant_key: str | None = Query(default=None)) -> TenantContext:
    """For the event-stream route only. The browser's EventSource cannot set headers, so the key may also arrive as
    ?tenant_key=. A key in a URL can end up in access logs: prefer the header (fetch-based streaming) where possible."""
    return tenant_context(x_tenant_key or tenant_key)


__all__ = ["TenantContext", "create_tenant", "resolve_key", "delete_tenant", "tenant_context", "tenant_context_sse",
           "tenant_seed", "tenant_name", "list_tenants", "graph_name"]
