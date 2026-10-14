import pytest
from ulid import ULID

from api.graph.client import TenantGraph, get_db, ping
from api.graph.schema import init_graph


@pytest.fixture(scope="session", autouse=True)
def require_falkordb():
    if not ping():
        pytest.fail("FalkorDB unreachable: run `docker compose up -d` (host port 6380)")


@pytest.fixture
def graph():
    """A throwaway tenant graph, deleted after the test."""
    g = TenantGraph("test_" + str(ULID()).lower()[-8:])
    init_graph(g, embed_dim=4)
    yield g
    g.delete()


@pytest.fixture
def dag(graph):
    """gw -> checkout -> payments -> ledger, gw -> refund -> payments (5 services, all edges current)."""
    graph.write("""
        CREATE (gw:Service {name:'gw', tier:'critical'}),
               (co:Service {name:'checkout', tier:'critical'}),
               (rf:Service {name:'refund', tier:'standard'}),
               (pay:Service {name:'payments', tier:'critical'}),
               (led:Service {name:'ledger', tier:'critical'}),
               (gw)-[:DEPENDS_ON {valid_from: 0}]->(co),
               (gw)-[:DEPENDS_ON {valid_from: 0}]->(rf),
               (co)-[:DEPENDS_ON {valid_from: 0}]->(pay),
               (rf)-[:DEPENDS_ON {valid_from: 0}]->(pay),
               (pay)-[:DEPENDS_ON {valid_from: 0}]->(led)
    """)
    return graph
