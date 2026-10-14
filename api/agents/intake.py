"""Incident intake: the page that starts an incident. Like a monitoring system paging on-call, it is not an agent step.

It reads the current alerts, picks the origin (the Service whose alert is strongest), opens the Incident with its
AFFECTS edges and writes the observed symptoms. Agents start from this subgraph.
"""

from api.graph import queries as q
from api.memory import episodic as ep
from api.sim.world import THRESHOLD, World

# crashloop / cert / saturation are local signals: they do not propagate up the call graph, so they point at the origin
LOCAL_KINDS = {"crashloop", "cert", "saturation"}
LOCAL_BONUS = 10.0


def strength(alert: dict) -> float:
    base = 1.0 if alert["metric"] == "cert_days_left" else alert["value"] / THRESHOLD[alert["metric"]]
    return base + (LOCAL_BONUS if alert["kind"] in LOCAL_KINDS else 0.0)


def pick_origin(world: World, alerts: list[dict]) -> str:
    services = [a for a in alerts if a["service"] in world.c.services]
    if not services:
        raise LookupError("page has no alert on a Service")
    return max(services, key=lambda a: (strength(a), a["service"]))["service"]


def open_from_page(g, world: World, iid: str) -> dict:
    """Open the incident for the current alerts. Returns {origin, alerts}. Does not advance the clock."""
    alerts = world.get_alerts(iid)
    origin = pick_origin(world, alerts)
    top = max((a for a in alerts if a["service"] == origin), key=strength)
    ep.open_incident(g, iid, f"{origin}: {top['kind']} alert", "sev3", world.now_ms, origin)
    # only alerting services that really sit in the origin's blast radius are "impacted"; an unrelated alert (red
    # herring) stays an observed symptom but must not be recorded as affected
    blast = {r["service"] for r in g.rows(q.Q1_BLAST_RADIUS, {"svc": origin})}
    ep.open_incident(g, iid, "", "sev3", world.now_ms, origin,
                     impacted=sorted({a["service"] for a in alerts if a["service"] in blast}))
    ep.add_symptoms(g, iid, alerts)
    return {"origin": origin, "alerts": alerts}
