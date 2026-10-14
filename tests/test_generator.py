from api.graph import queries as q
from api.sim.generator import SIM_NOW, T0, build_company, seed_tenant


def test_company_shape_matches_prd():
    c = build_company(42)
    assert len(c.services) == 40
    assert sum(s["tier"] == "critical" for s in c.services.values()) == 12
    assert len({s["domain"] for s in c.services.values()}) == 6
    assert len(c.datastores) == 8
    assert sorted(d["kind"] for d in c.datastores.values()).count("postgres") == 4
    assert len(c.teams) == 7 and len(c.people) == 28
    assert len(c.flags) == 25
    assert 90 <= len(c.edges) <= 130, len(c.edges)
    assert len(c.deploys) == 300
    changed = sum(bool(d["flags_changed"]) for d in c.deploys)
    assert 0.15 <= changed / 300 <= 0.25
    per_team = {}
    for s in c.services.values():
        per_team[s["team"]] = per_team.get(s["team"], 0) + 1
    assert all(4 <= n <= 8 for n in per_team.values()), per_team


def test_same_seed_same_company_different_seed_different_topology():
    a, b, other = build_company(42), build_company(42), build_company(7)
    assert a.edges == b.edges and a.deploys == b.deploys
    assert {(e["from"], e["to"]) for e in a.edges} != {(e["from"], e["to"]) for e in other.edges}
    assert set(a.services) == set(other.services)  # overlapping names across tenants


def test_topology_is_a_dag_of_depth_at_most_6():
    c = build_company(42)
    depth: dict[str, int] = {}

    def d(n: str, stack=()) -> int:
        assert n not in stack, f"cycle through {n}"
        if n not in depth:
            depth[n] = max((1 + d(x, stack + (n,)) for x in c.deps_of(n)), default=0)
        return depth[n]

    assert max(d(n) for n in c.services) <= 6
    # every service is reachable from the gateway
    reach = set(c.deps_of("api-gateway"))
    frontier = list(reach)
    while frontier:
        for x in c.deps_of(frontier.pop()):
            if x not in reach:
                reach.add(x)
                frontier.append(x)
    assert set(c.services) - {"api-gateway"} <= reach


def test_history_is_inside_the_30_day_window():
    c = build_company(42)
    assert all(T0 <= d["at"] < SIM_NOW for d in c.deploys)


def test_seed_tenant_loads_graph(graph):
    c = seed_tenant(graph, 42)
    assert graph.read("MATCH (s:Service) RETURN count(s)") == [[40]]
    assert graph.read("MATCH ()-[r:DEPENDS_ON]->() RETURN count(r)") == [[len(c.edges)]]
    assert graph.read("MATCH (d:Deploy) RETURN count(d)") == [[300]]
    assert graph.read("MATCH (d:Deploy) WHERE size(d.flags_changed) > 0 RETURN count(d)")[0][0] > 0
    # blast radius of the ledger datastore's callers is non-trivial and ordered by hops
    rows = graph.rows(q.Q1_BLAST_RADIUS, {"svc": "ledger-writer"})
    assert rows and rows[0]["hops"] == 1
    assert "checkout-api" in {r["service"] for r in rows}
    owners = graph.rows(q.Q8_OWNERS, {"svc": "checkout-api"})
    assert owners[0]["team"] == "Checkout" and len(owners[0]["members"]) == 4
    # pagerank was computed
    assert graph.read("MATCH (s:Service) WHERE s.pagerank IS NULL RETURN count(s)") == [[0]]
