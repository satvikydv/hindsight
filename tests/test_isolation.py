"""Isolation by construction (PRD §11/§12): one graph per tenant, graph name derived from the API key only."""

import pytest
from fastapi.testclient import TestClient

from api.graph import queries as q
from api.graph.client import TenantGraph, get_db, graph_name
from api.main import create_app
from api.sim.generator import seed_tenant
from api.sim.world import World, write_events
from api.tenancy import create_tenant, delete_tenant, resolve_key


@pytest.fixture
def two_tenants():
    a_id, a_key = create_tenant("iso a")
    b_id, b_key = create_tenant("iso b")
    ga, gb = TenantGraph(a_id), TenantGraph(b_id)
    ca, cb = seed_tenant(ga, 42), seed_tenant(gb, 7)  # same service names, different topology
    yield (a_id, a_key, ga, ca), (b_id, b_key, gb, cb)
    delete_tenant(a_id)
    delete_tenant(b_id)


def all_ids(g: TenantGraph) -> set[str]:
    return {r[0] for r in g.read("MATCH (n) WHERE n.id IS NOT NULL RETURN n.id")}


def test_tenants_overlap_on_service_names_but_share_no_node_ids(two_tenants):
    (_, _, ga, _), (_, _, gb, _) = two_tenants
    names = lambda g: {r[0] for r in g.read("MATCH (s:Service) RETURN s.name")}
    assert names(ga) == names(gb) and len(names(ga)) == 40
    # the only ids two tenants share are the 4 fixed agent ids every tenant has; every ULID differs
    assert all_ids(ga) & all_ids(gb) == {f"agent_{n}" for n in ("triage", "investigator", "remediator", "scribe")}


def test_concurrent_incidents_never_leak_across_tenants(two_tenants):
    (_, _, ga, ca), (_, _, gb, cb) = two_tenants
    wa, wb = World(ca, 1), World(cb, 1)
    write_events(ga, wa.inject("i1", "bad_deploy", "checkout-api"))
    write_events(gb, wb.inject("i1", "bad_deploy", "checkout-api"))  # same incident id, same service name
    ga.write("CREATE (:Playbook {id:'pb_secret_a', name:'only in A', status:'active', weight:0.9})")

    assert gb.read("MATCH (p:Playbook) RETURN count(p)") == [[0]]
    assert gb.read("MATCH (n {id:'pb_secret_a'}) RETURN count(n)") == [[0]]
    deploy_a = {r[0] for r in ga.read("MATCH (d:Deploy) WHERE d.source = 'sim' RETURN d.id")}
    deploy_b = {r[0] for r in gb.read("MATCH (d:Deploy) WHERE d.source = 'sim' RETURN d.id")}
    assert len(deploy_a) == len(deploy_b) == 1 and not deploy_a & deploy_b
    # same query, same params, different graphs: results come only from the graph it ran on
    ra = {r["service"] for r in ga.rows(q.Q1_BLAST_RADIUS, {"svc": "ledger-writer"})}
    rb = {r["service"] for r in gb.rows(q.Q1_BLAST_RADIUS, {"svc": "ledger-writer"})}
    assert ra and rb and ra != rb


def test_api_key_binds_the_graph_and_bad_keys_get_401(two_tenants):
    (a_id, a_key, _, _), (b_id, b_key, gb, _) = two_tenants
    gb.write("CREATE (:Secret {id:'only_in_b'})")
    with TestClient(create_app(resume=False, demo=False)) as client:
        assert client.get("/tenant", headers={"X-Tenant-Key": a_key}).json()["tenant_id"] == a_id
        rb = client.get("/tenant", headers={"X-Tenant-Key": b_key}).json()
        assert rb["tenant_id"] == b_id
        assert client.get("/tenant", headers={"X-Tenant-Key": "hk_nope"}).status_code == 401
        assert client.get("/tenant").status_code == 401
        # a tenant id smuggled in as a header or query param changes nothing
        r = client.get(f"/tenant?tenant_id={b_id}", headers={"X-Tenant-Key": a_key, "X-Tenant-Id": b_id})
        assert r.json()["tenant_id"] == a_id


def test_graph_name_is_validated_and_keys_are_not_stored_in_clear(two_tenants):
    (a_id, a_key, _, _), _ = two_tenants
    for bad in ("x; drop", "../other", "A B", "", "a" * 41):
        with pytest.raises(ValueError):
            graph_name(bad)
    assert resolve_key(a_key) == a_id and resolve_key(None) is None and resolve_key("") is None
    stored = get_db().select_graph("_registry").ro_query("MATCH (t:Tenant) RETURN t.key_hash").result_set
    assert a_key not in {r[0] for r in stored} and all(len(r[0]) == 64 for r in stored)


def test_deleting_a_tenant_removes_its_graph():
    t_id, key = create_tenant("iso tmp")
    assert graph_name(t_id) in get_db().list_graphs()
    delete_tenant(t_id)
    assert graph_name(t_id) not in get_db().list_graphs()
    assert resolve_key(key) is None
