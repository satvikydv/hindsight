"""API key -> tenant graph. The graph name is derived here, server-side, and nowhere else."""

import hashlib
import re
import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException
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


def create_tenant(name: str) -> tuple[str, str]:
    """Create an empty, indexed tenant graph. Returns (tenant_id, api_key); the key is shown once."""
    tenant_id = f"{_slug(name)}_{str(ULID()).lower()[-6:]}"
    key = "hk_" + secrets.token_urlsafe(24)
    _registry().query(
        "CREATE (:Tenant {id: $id, name: $name, key_hash: $h})",
        {"id": tenant_id, "name": name, "h": _hash(key)},
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


def delete_tenant(tenant_id: str) -> None:
    TenantGraph(tenant_id).delete()
    _registry().query("MATCH (t:Tenant {id: $id}) DELETE t", {"id": tenant_id})


def tenant_context(x_tenant_key: str | None = Header(default=None)) -> TenantContext:
    """FastAPI dependency. Unknown or missing key -> 401. No code path takes a graph name from a request."""
    tenant_id = resolve_key(x_tenant_key)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="unknown tenant key")
    return TenantContext(tenant_id=tenant_id, graph=TenantGraph(tenant_id))


__all__ = ["TenantContext", "create_tenant", "resolve_key", "delete_tenant", "tenant_context", "graph_name"]
