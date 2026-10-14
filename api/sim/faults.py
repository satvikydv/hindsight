"""Fault catalog (PRD §9): what each fault class is injected as, which targets are valid, and the ordered fix."""

from dataclasses import dataclass

from api.sim.generator import Company


@dataclass(frozen=True)
class FaultClass:
    name: str
    injected_as: str
    symptoms: str
    correct_fix: str


CATALOG: dict[str, FaultClass] = {c.name: c for c in [
    FaultClass("bad_deploy", "New Deploy on a service with a bug", "error_rate up on service and callers",
               "rollback(service)"),
    FaultClass("bad_flag", "ConfigFlag change", "latency or error_rate up on the flag owner and callers",
               "set_flag(key, previous)"),
    FaultClass("db_pool_exhaustion", "Datastore saturation", "saturation up on datastore, latency up on dependents",
               "scale(datastore) then restart(top caller)"),
    FaultClass("memory_leak", "Service memory climbs", "crashloop and restarts",
               "restart(service), then rollback if it recurs"),
    FaultClass("cert_expiry", "Cert on a service expires", "TLS errors at callers", "renew_cert(service)"),
    FaultClass("dependency_outage", "Downstream service hard-down", "errors on every caller", "failover(service)"),
    FaultClass("traffic_spike", "Load on gateway or an edge API", "latency up across tier-critical services",
               "scale(service)"),
    FaultClass("cascading", "db_pool_exhaustion and bad_deploy at once", "mixed",
               "both fixes, deepest first"),
]}

FAULT_CLASSES = list(CATALOG)
EDGE_LAYER = 1  # edge APIs sit directly under the gateway


def candidates(c: Company, fault_class: str) -> list[str]:
    """Valid targets for a fault class, in a stable order."""
    services = [n for n in c.services if n != "api-gateway"]
    if fault_class in ("bad_deploy", "memory_leak", "cert_expiry", "dependency_outage"):
        return services
    if fault_class == "bad_flag":
        owners = {f["service"] for f in c.flags.values()}
        return [n for n in c.services if n in owners]
    if fault_class in ("db_pool_exhaustion", "cascading"):
        return [n for n, d in c.datastores.items() if d["kind"] in ("postgres", "redis")]
    if fault_class == "traffic_spike":
        return ["api-gateway"] + [n for n, s in c.services.items() if s["layer"] == EDGE_LAYER]
    raise ValueError(f"unknown fault class {fault_class!r}")


def bad_value(value: str) -> str:
    """The broken value a bad_flag change sets."""
    if value.lstrip("-").isdigit():
        return str(int(value) * 10)
    flips = {"true": "false", "false": "true", "on": "off", "off": "on"}
    return flips.get(value, value + "-bad")
