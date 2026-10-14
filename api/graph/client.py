"""FalkorDB access. One named graph per tenant; the graph name never comes from a request or the model."""

import re
import time

from falkordb import FalkorDB
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from api import config

_TENANT_ID = re.compile(r"^[a-z0-9_]{1,40}$")
_RETRY_DELAYS = (0.1, 0.4, 0.8)  # PRD §11: 3 tries, 100 -> 800 ms, connection errors only

_db: FalkorDB | None = None


def get_db() -> FalkorDB:
    global _db
    if _db is None:
        _db = FalkorDB(
            host=config.FALKORDB_HOST,
            port=config.FALKORDB_PORT,
            password=config.FALKORDB_PASSWORD,
            max_connections=config.POOL_SIZE,
        )
    return _db


def graph_name(tenant_id: str) -> str:
    if not _TENANT_ID.match(tenant_id):
        raise ValueError(f"bad tenant id: {tenant_id!r}")
    return f"tenant_{tenant_id}"


class TenantGraph:
    """Thin wrapper over one tenant graph: ro_query for reads, query for writes, retry on connection errors only."""

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self.name = graph_name(tenant_id)
        self._g = get_db().select_graph(self.name)

    def _run(self, fn, q: str, params: dict | None):
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                return fn(q, params or {})
            except (RedisConnectionError, RedisTimeoutError):
                if attempt == len(_RETRY_DELAYS):
                    raise
                time.sleep(_RETRY_DELAYS[attempt])

    def read(self, q: str, params: dict | None = None) -> list[list]:
        return self._run(self._g.ro_query, q, params).result_set

    def write(self, q: str, params: dict | None = None) -> list[list]:
        return self._run(self._g.query, q, params).result_set

    def rows(self, q: str, params: dict | None = None, write: bool = False) -> list[dict]:
        """Result set as dicts keyed by the RETURN aliases."""
        res = self._run(self._g.query if write else self._g.ro_query, q, params)
        cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
        return [dict(zip(cols, row)) for row in res.result_set]

    def delete(self) -> None:
        self._g.delete()


def tenant_exists(tenant_id: str) -> bool:
    return graph_name(tenant_id) in get_db().list_graphs()


def ping() -> bool:
    try:
        get_db().list_graphs()
        return True
    except Exception:
        return False
