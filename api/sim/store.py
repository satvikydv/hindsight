"""Where the simulator's ground truth is persisted: a JSON file per tenant, outside the graph (agents can't read it).

Writes are atomic (temp file + rename) so a crash mid-write never leaves a half-written snapshot.
"""

import json
import os
from pathlib import Path

from api.sim.generator import Company
from api.sim.world import World

STORE_DIR = Path(__file__).resolve().parents[2] / "data" / "sim"


def _path(tenant_id: str) -> Path:
    return STORE_DIR / f"{tenant_id}.json"


def save(tenant_id: str, world: World) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(tenant_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(world.snapshot()), encoding="utf-8")
    os.replace(tmp, _path(tenant_id))


def load(tenant_id: str, company: Company) -> World | None:
    p = _path(tenant_id)
    return World.restore(company, json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None


def delete(tenant_id: str) -> None:
    _path(tenant_id).unlink(missing_ok=True)
