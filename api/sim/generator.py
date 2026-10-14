"""Kestrel Pay generator: a fictional fintech, fully synthetic (hackathon rule 6).

Same seed -> same company. `build_company(seed)` is pure Python; `seed_tenant` writes it to a tenant graph.
Topology (edges) depends on the seed; names, tiers and ownership are fixed so tenants overlap on service names.
"""

import argparse
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ulid import ULID

from api.graph import queries
from api.graph.client import TenantGraph
from api.graph.schema import init_graph

DAY_MS = 86_400_000
T0 = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)  # simulated history starts here
HISTORY_DAYS = 30
SIM_NOW = T0 + HISTORY_DAYS * DAY_MS

# (name, domain, tier, layer, team). Layer 0 is the top of the call graph; datastores sit below layer 4.
SERVICES = [
    ("api-gateway", "platform", "critical", 0, "Platform"),
    ("checkout-api", "payments", "critical", 1, "Checkout"),
    ("refund-api", "payments", "critical", 1, "Checkout"),
    ("merchant-api", "payments", "standard", 1, "Checkout"),
    ("login-api", "identity", "critical", 1, "Identity"),
    ("account-api", "identity", "standard", 1, "Identity"),
    ("notification-api", "notifications", "standard", 1, "Notifications"),
    ("payment-orchestrator", "payments", "critical", 2, "Payments Core"),
    ("card-processor", "payments", "critical", 2, "Payments Core"),
    ("payout-service", "payments", "standard", 2, "Checkout"),
    ("invoice-service", "payments", "standard", 2, "Checkout"),
    ("dispute-service", "payments", "standard", 2, "Payments Core"),
    ("auth-service", "identity", "critical", 2, "Identity"),
    ("session-service", "identity", "critical", 2, "Identity"),
    ("kyc-service", "identity", "standard", 2, "Identity"),
    ("profile-service", "identity", "standard", 2, "Identity"),
    ("email-sender", "notifications", "standard", 2, "Notifications"),
    ("sms-sender", "notifications", "standard", 2, "Notifications"),
    ("push-sender", "notifications", "standard", 2, "Notifications"),
    ("template-service", "notifications", "standard", 2, "Notifications"),
    ("fraud-scorer", "risk", "critical", 2, "Risk"),
    ("rules-engine", "risk", "standard", 2, "Risk"),
    ("device-fingerprint", "risk", "standard", 2, "Risk"),
    ("ledger-writer", "ledger", "critical", 3, "Ledger"),
    ("ledger-reader", "ledger", "critical", 3, "Ledger"),
    ("balance-service", "ledger", "standard", 3, "Ledger"),
    ("reconciliation-service", "ledger", "standard", 3, "Ledger"),
    ("fx-rates", "payments", "standard", 3, "Payments Core"),
    ("tokenizer", "payments", "standard", 3, "Payments Core"),
    ("limits-service", "risk", "standard", 3, "Risk"),
    ("risk-profile-service", "risk", "standard", 3, "Risk"),
    ("config-service", "platform", "critical", 4, "Platform"),
    ("feature-flags", "platform", "standard", 4, "Platform"),
    ("secrets-broker", "platform", "standard", 4, "Platform"),
    ("event-bus-adapter", "platform", "standard", 4, "Platform"),
    ("audit-logger", "platform", "standard", 4, "Ledger"),
    ("scheduler", "platform", "standard", 4, "Payments Core"),
    ("cert-manager", "platform", "standard", 4, "Platform"),
    ("metrics-collector", "platform", "standard", 4, "Platform"),
    ("ledger-archiver", "ledger", "standard", 4, "Ledger"),
]

DATASTORES = [
    ("ledger-db", "postgres"), ("payments-db", "postgres"), ("identity-db", "postgres"), ("risk-db", "postgres"),
    ("session-cache", "redis"), ("rate-limit-cache", "redis"), ("event-stream", "kafka"), ("document-store", "s3"),
]

TEAMS = ["Checkout", "Payments Core", "Identity", "Ledger", "Risk", "Notifications", "Platform"]

# Config flags: (key, default value, owning service). 25 total.
FLAGS = [
    ("checkout.retry_limit", "3", "checkout-api"), ("checkout.timeout_ms", "2500", "checkout-api"),
    ("refund.retry_limit", "3", "refund-api"), ("refund.batch_size", "50", "refund-api"),
    ("orchestrator.circuit_breaker", "on", "payment-orchestrator"),
    ("orchestrator.max_inflight", "200", "payment-orchestrator"),
    ("card.3ds_enabled", "true", "card-processor"), ("card.acquirer_timeout_ms", "4000", "card-processor"),
    ("tokenizer.cache_ttl_s", "300", "tokenizer"),
    ("risk.strict_mode", "false", "fraud-scorer"), ("risk.model_version", "v7", "fraud-scorer"),
    ("rules.max_rules", "500", "rules-engine"), ("limits.daily_cap", "10000", "limits-service"),
    ("auth.token_ttl_s", "900", "auth-service"), ("session.max_sessions", "5", "session-service"),
    ("login.captcha", "true", "login-api"), ("kyc.provider", "primary", "kyc-service"),
    ("ledger.write_batch", "100", "ledger-writer"), ("ledger.read_replica", "on", "ledger-reader"),
    ("balance.cache_ttl_s", "30", "balance-service"),
    ("gateway.rate_limit_rps", "2000", "api-gateway"), ("gateway.shed_load", "off", "api-gateway"),
    ("notify.retry_limit", "5", "notification-api"), ("email.provider", "ses", "email-sender"),
    ("config.refresh_s", "60", "config-service"),
]

# Edges that must exist so the demo story has shared structure (checkout-api and refund-api share a dependency).
FORCED_EDGES = [
    ("api-gateway", "checkout-api"), ("api-gateway", "refund-api"),
    ("checkout-api", "payment-orchestrator"), ("checkout-api", "fraud-scorer"),
    ("refund-api", "payment-orchestrator"), ("refund-api", "ledger-writer"),
    ("payment-orchestrator", "ledger-writer"), ("payment-orchestrator", "card-processor"),
]

DIFF_SUMMARIES = [
    "bump http client, tune retry backoff", "refactor request validation", "add idempotency key handling",
    "upgrade database driver", "change connection pool settings", "new feature behind flag",
    "fix timezone bug in settlement window", "update dependency versions", "cache hot path lookups",
    "tighten input schema", "remove deprecated endpoint", "add structured logging",
]

FIRST = ["Ana", "Ben", "Chen", "Dara", "Eli", "Fatima", "Gus", "Hana", "Ivan", "Jia", "Kofi", "Lena", "Mateo",
         "Nia", "Omar", "Priya", "Quinn", "Ravi", "Sofia", "Tariq", "Uma", "Vik", "Wen", "Xena", "Yusuf", "Zoe",
         "Arjun", "Bela", "Cleo", "Dev", "Esme", "Farid"]
LAST = ["Alvarez", "Brooks", "Chowdhury", "Dubois", "Eriksen", "Farouk", "Gupta", "Haddad", "Ito", "Jensen",
        "Kapoor", "Lindqvist", "Mbeki", "Novak", "Okafor", "Petrov", "Quispe", "Rossi", "Sato", "Tanaka"]
ROLES = ["EM", "SRE", "SWE", "SWE"]  # per team of 4
RUNTIMES = ["jvm", "go", "node", "python"]


@dataclass
class Company:
    seed: int
    services: dict[str, dict] = field(default_factory=dict)      # name -> props (incl. id, layer, team)
    datastores: dict[str, dict] = field(default_factory=dict)
    teams: dict[str, dict] = field(default_factory=dict)
    people: list[dict] = field(default_factory=list)
    deploys: list[dict] = field(default_factory=list)
    flags: dict[str, dict] = field(default_factory=dict)         # key -> props (incl. service)
    edges: list[dict] = field(default_factory=list)              # {from, to, to_label, kind}

    def callers_of(self, name: str) -> list[str]:
        return [e["from"] for e in self.edges if e["to"] == name]

    def deps_of(self, name: str) -> list[str]:
        return [e["to"] for e in self.edges if e["from"] == name]

    def ancestors(self, name: str, max_hops: int = 4) -> dict[str, int]:
        """Transitive callers of `name` with hop distance (the blast radius, computed in Python for the simulator)."""
        seen: dict[str, int] = {}
        frontier = [name]
        for hop in range(1, max_hops + 1):
            nxt = []
            for n in frontier:
                for c in self.callers_of(n):
                    if c not in seen:
                        seen[c] = hop
                        nxt.append(c)
            frontier = nxt
        return seen

    def kind_of(self, name: str) -> str:
        return "service" if name in self.services else "datastore"


def _id(rng: random.Random) -> str:
    return str(ULID.from_bytes(rng.randbytes(16)))


def build_company(seed: int) -> Company:
    rng = random.Random(seed)
    c = Company(seed=seed)

    for name, domain, tier, layer, team in SERVICES:
        c.services[name] = {"id": _id(rng), "name": name, "tier": tier, "domain": domain, "layer": layer,
                            "team": team, "runtime": rng.choice(RUNTIMES)}
    for name, kind in DATASTORES:
        c.datastores[name] = {"id": _id(rng), "name": name, "kind": kind}
    for t in TEAMS:
        c.teams[t] = {"id": _id(rng), "name": t, "slack_channel": "#team-" + t.lower().replace(" ", "-")}

    used = set()
    for t in TEAMS:
        for role in ROLES:
            while True:
                nm = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
                if nm not in used:
                    used.add(nm)
                    break
            c.people.append({"id": _id(rng), "name": nm, "role": role, "team": t,
                             "handle": nm.lower().replace(" ", ".")})

    for key, value, svc in FLAGS:
        c.flags[key] = {"id": _id(rng), "key": key, "value": value, "service": svc}

    _build_edges(c, rng)
    _build_deploys(c, rng)
    return c


def _build_edges(c: Company, rng: random.Random) -> None:
    by_layer: dict[int, list[str]] = {}
    for name, s in c.services.items():
        by_layer.setdefault(s["layer"], []).append(name)
    store_names = list(c.datastores)
    pairs: set[tuple[str, str]] = set(FORCED_EDGES)

    for name, s in c.services.items():
        layer = s["layer"]
        if layer == 0:
            pairs.update((name, d) for d in by_layer[1])
            continue
        lower = [n for l in range(layer + 1, 5) for n in by_layer.get(l, [])]
        # edge APIs call a few of the layer below; deeper services call fewer things
        k_services = {1: rng.randint(2, 3), 2: rng.randint(1, 3), 3: rng.randint(1, 2), 4: rng.randint(0, 1)}[layer]
        if lower:
            nearer = [n for n in lower if c.services[n]["layer"] == layer + 1] or lower
            pool = nearer if rng.random() < 0.7 else lower
            pairs.update((name, d) for d in rng.sample(pool, min(k_services, len(pool))))
        if rng.random() < (0.3 if layer == 1 else 0.7):
            pairs.add((name, rng.choice(store_names)))

    # every non-gateway service must have at least one caller, or no incident could ever reach it
    called = {b for _, b in pairs}
    for name, s in c.services.items():
        if s["layer"] > 0 and name not in called:
            shallower = [n for n, x in c.services.items() if x["layer"] == s["layer"] - 1]
            pairs.add((rng.choice(shallower), name))
    for store in store_names:
        if not any(b == store for _, b in pairs):
            pairs.add((rng.choice(list(c.services)), store))

    for a, b in sorted(pairs):
        c.edges.append({"from": a, "to": b, "to_label": "Service" if b in c.services else "Datastore",
                        "kind": rng.choice(["sync", "sync", "async"])})


def _build_deploys(c: Company, rng: random.Random, n: int = 300) -> None:
    names = [s for s in c.services]
    flagged = {f["service"] for f in c.flags.values()}
    team_people = {t: [p for p in c.people if p["team"] == t] for t in TEAMS}
    raw = []
    for _ in range(n):
        svc = rng.choice(names)
        at = T0 + rng.randrange(0, HISTORY_DAYS * DAY_MS - DAY_MS)  # nothing in the last day: the story starts fresh
        author = rng.choice(team_people[c.services[svc]["team"]])
        raw.append({"service": svc, "at": at, "author": author["id"], "diff_summary": rng.choice(DIFF_SUMMARIES)})
    raw.sort(key=lambda d: d["at"])
    counters: dict[str, int] = {}
    for d in raw:
        counters[d["service"]] = counters.get(d["service"], 0) + 1
        d["id"] = _id(rng)
        d["version"] = f"v1.{counters[d['service']]}.0"
        d["flags_changed"] = []
    # ~20% of all deploys change 1-3 flags; only services that own flags can
    candidates = [d for d in raw if d["service"] in flagged]
    for d in rng.sample(candidates, min(len(candidates), n // 5)):
        own = [k for k, f in c.flags.items() if f["service"] == d["service"]]
        d["flags_changed"] = rng.sample(own, rng.randint(1, min(3, len(own))))
    c.deploys = raw


def seed_tenant(g: TenantGraph, seed: int) -> Company:
    """Write the company to an (empty) tenant graph and return it for the simulator."""
    c = build_company(seed)
    init_graph(g)

    def load(label: str, rows: list[dict]) -> None:
        g.write(f"UNWIND $rows AS r CREATE (n:{label}) SET n = r, n.created_at = $t, n.source = 'seed'",
                {"rows": rows, "t": T0})

    load("Service", [{k: v for k, v in s.items() if k not in ("layer", "team")} for s in c.services.values()])
    load("Datastore", list(c.datastores.values()))
    load("Team", list(c.teams.values()))
    load("Person", [{k: v for k, v in p.items() if k != "team"} for p in c.people])
    load("ConfigFlag", [{k: v for k, v in f.items() if k != "service"} for f in c.flags.values()])
    load("Deploy", [{k: v for k, v in d.items() if k not in ("service", "author")} for d in c.deploys])
    g.write("UNWIND $rows AS r CREATE (:Agent {id: r.id, name: r.name, version: 1, created_at: $t, source: 'seed'})",
            {"rows": [{"id": f"agent_{n}", "name": n} for n in ("triage", "investigator", "remediator", "scribe")],
             "t": T0})

    e = {"t": T0}
    for label in ("Service", "Datastore"):
        rows = [{"a": c.services[x["from"]]["id"], "b": (c.services.get(x["to"]) or c.datastores[x["to"]])["id"],
                 "kind": x["kind"]} for x in c.edges if x["to_label"] == label]
        g.write(f"UNWIND $rows AS r MATCH (a:Service {{id: r.a}}) MATCH (b:{label} {{id: r.b}}) "
                "CREATE (a)-[:DEPENDS_ON {kind: r.kind, valid_from: $t, source: 'seed'}]->(b)", {"rows": rows, **e})
    g.write("UNWIND $rows AS r MATCH (t:Team {id: r.t}) MATCH (s:Service {id: r.s}) "
            "CREATE (t)-[:OWNS {valid_from: $t0, source: 'seed'}]->(s)",
            {"rows": [{"t": c.teams[s["team"]]["id"], "s": s["id"]} for s in c.services.values()], "t0": T0})
    g.write("UNWIND $rows AS r MATCH (p:Person {id: r.p}) MATCH (t:Team {id: r.t}) "
            "CREATE (p)-[:MEMBER_OF {valid_from: $t0, source: 'seed'}]->(t)",
            {"rows": [{"p": p["id"], "t": c.teams[p["team"]]["id"]} for p in c.people], "t0": T0})
    g.write("UNWIND $rows AS r MATCH (d:Deploy {id: r.d}) MATCH (s:Service {id: r.s}) MATCH (p:Person {id: r.p}) "
            "CREATE (d)-[:DEPLOYED]->(s) CREATE (p)-[:AUTHORED]->(d)",
            {"rows": [{"d": d["id"], "s": c.services[d["service"]]["id"], "p": d["author"]} for d in c.deploys]})
    g.write("UNWIND $rows AS r MATCH (s:Service {id: r.s}) MATCH (f:ConfigFlag {id: r.f}) "
            "CREATE (s)-[:HAS_FLAG {valid_from: $t0}]->(f)",
            {"rows": [{"s": c.services[f["service"]]["id"], "f": f["id"]} for f in c.flags.values()], "t0": T0})

    g.write(queries.Q3_PAGERANK)
    return c


def main() -> None:
    from api.tenancy import create_tenant

    ap = argparse.ArgumentParser(description="Create and seed a Kestrel Pay tenant")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--tenant", default="acme")
    a = ap.parse_args()
    tenant_id, key = create_tenant(a.tenant)
    c = seed_tenant(TenantGraph(tenant_id), a.seed)
    print(f"tenant_id={tenant_id}\napi_key={key}\n{len(c.services)} services, {len(c.edges)} edges, "
          f"{len(c.deploys)} deploys (seed {a.seed})")


if __name__ == "__main__":
    main()
