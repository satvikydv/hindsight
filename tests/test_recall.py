import pytest
from ulid import ULID

from api.graph.client import TenantGraph
from api.graph.schema import init_graph
from api.ids import new_id
from api.llm import DownEmbedder, EmbeddingUnavailable, FakeEmbedder
from api.memory import episodic as ep
from api.memory import procedural as pr
from api.memory import recall as rc
from api.sim.generator import SIM_NOW, seed_tenant

DAY = 86_400_000
ROLLBACK = [{"tool": "rollback", "args_template": {"service": "$origin"}}]


@pytest.fixture
def g():
    g = TenantGraph("test_" + str(ULID()).lower()[-8:])
    init_graph(g, embed_dim=64)
    seed_tenant(g, 42)
    yield g
    g.delete()


class ControlledEmbedder(FakeEmbedder):
    """Exact vectors for the story so the tests check the ranking pipeline, not embedding-model quality (that is an
    empirical result for the benchmark). Texts starting with 'payment refunds' sit at e1; the checkout deploy incident
    sits at cosine 0.3 from e1; anything else falls back to the bag-of-words fake."""

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            if t.startswith("payment refunds"):
                v[0] = 1.0
            elif t.startswith("checkout-api errors after deploy"):
                v[0], v[1] = 0.3, (1 - 0.3 ** 2) ** 0.5
            else:
                v = super().embed([t])[0]
            out.append(v)
        return out


emb = ControlledEmbedder(dim=64)


def deploy_on(g, service):
    return g.read("MATCH (d:Deploy)-[:DEPLOYED]->(:Service {name:$s}) RETURN d.id LIMIT 1", {"s": service})[0][0]


def make_incident(g, title, origin, symptoms, resolved_at=None, cause=("Deploy", None), index=True):
    """symptoms: [(service, kind)]. resolved_at=None leaves it open."""
    iid = new_id()
    ep.open_incident(g, iid, title, "sev2", SIM_NOW - 100 * DAY, origin)
    ep.add_symptoms(g, iid, [{"service": s, "kind": k, "metric": k, "value": 1.0} for s, k in symptoms])
    if resolved_at is not None:
        label, node = cause
        ep.confirm_root_cause(g, iid, label, node or deploy_on(g, origin), "investigator")
        ep.set_status(g, iid, "resolved", resolved_at)
    if index:
        assert rc.index_incident(g, emb, iid)
    return iid


NOW = SIM_NOW + DAY


@pytest.fixture
def story(g):
    """The constructed case the design is for. Current incident: refund-api (never failed) throwing errors.
    - checkout: a past bad deploy on checkout-api. Different words, same shape (payments, critical, shared dependency,
      error_rate on service and gateway). Has a playbook.
    - distractor: same title as the current incident, but an unrelated ledger saturation. Text matches, shape doesn't.
    This illustrates the mechanism; the real ablation numbers come from the benchmark with real embeddings."""
    title = "payment refunds are failing"
    checkout = make_incident(g, "checkout-api errors after deploy", "checkout-api",
                             [("checkout-api", "error_rate"), ("api-gateway", "error_rate")], SIM_NOW)
    pb = pr.create_playbook(g, "rollback bad deploy", "bad_deploy", ROLLBACK, checkout, SIM_NOW)
    distractor = make_incident(g, title, "ledger-writer",
                               [("ledger-writer", "saturation"), ("payment-orchestrator", "latency")], SIM_NOW,
                               cause=("Datastore", g.read("MATCH (d:Datastore {name:'ledger-db'}) RETURN d.id")[0][0]))
    cur = make_incident(g, title, "refund-api", [("refund-api", "error_rate"), ("api-gateway", "error_rate")])
    return dict(checkout=checkout, distractor=distractor, cur=cur, pb=pb)


def test_graph_recall_finds_the_structurally_similar_incident_first_and_its_playbook(g, story):
    r = rc.recall_similar(g, emb, story["cur"], NOW, "graph")
    assert r.similar[0]["incident"] == story["checkout"]
    assert r.similar[0]["parts"]["structural"] > r.similar[1]["parts"]["structural"]
    assert r.similar[0]["parts"]["vector"] < r.similar[1]["parts"]["vector"]   # structure won despite lower text sim
    assert r.similar[0]["cause_types"] == ["Deploy"]
    assert [p["playbook"] for p in r.playbooks] == [story["pb"]]
    assert len(r.similar) <= rc.TOP_K and r.warnings == []


def test_vector_only_follows_the_words_and_picks_the_distractor(g, story):
    r = rc.recall_similar(g, emb, story["cur"], NOW, "vector")
    assert r.similar[0]["incident"] == story["distractor"]         # identical title, wrong shape, no useful playbook
    assert r.playbooks == [] and all(s["basis"] == "text" for s in r.similar)
    assert all(s["parts"]["structural"] == 0 for s in r.similar)


def test_off_mode_recalls_nothing_and_writes_nothing(g, story):
    r = rc.recall_similar(g, emb, story["cur"], NOW, "off")
    assert r.similar == [] and r.playbooks == []
    assert g.read("MATCH ()-[s:SIMILAR_TO]->() RETURN count(s)") == [[0]]
    with pytest.raises(ValueError):
        rc.recall_similar(g, emb, story["cur"], NOW, "bogus")


def test_similar_to_edges_are_written_once_with_score_and_basis(g, story):
    rc.recall_similar(g, emb, story["cur"], NOW, "graph")
    rc.recall_similar(g, emb, story["cur"], NOW, "graph")                # re-running must not duplicate
    rows = g.rows("MATCH (:Incident {id:$i})-[s:SIMILAR_TO]->(b:Incident) RETURN b.id AS b, s.score AS score, "
                  "s.basis AS basis ORDER BY score DESC", {"i": story["cur"]})
    assert rows[0]["b"] == story["checkout"] and rows[0]["basis"] in ("both", "structure") and rows[0]["score"] > 0
    assert len(rows) == len({r["b"] for r in rows}) <= rc.TOP_K


def test_cold_start_returns_empty_without_error(g):
    cur = make_incident(g, "first ever incident", "checkout-api", [("checkout-api", "error_rate")])
    r = rc.recall_similar(g, emb, cur, NOW, "graph")
    assert r.similar == [] and r.playbooks == [] and r.warnings == []


def test_open_incidents_and_the_incident_itself_are_never_recalled(g, story):
    still_open = make_incident(g, "checkout-api errors again", "checkout-api",
                               [("checkout-api", "error_rate"), ("api-gateway", "error_rate")])
    r = rc.recall_similar(g, emb, story["cur"], NOW, "graph")
    assert still_open not in {s["incident"] for s in r.similar} and story["cur"] not in {s["incident"] for s in r.similar}
    v = rc.recall_similar(g, emb, story["cur"], NOW, "vector")
    assert still_open not in {s["incident"] for s in v.similar} and story["cur"] not in {s["incident"] for s in v.similar}


def test_embeddings_down_falls_back_to_structure_with_a_warning(g, story):
    r = rc.recall_similar(g, DownEmbedder(), story["cur"], NOW, "graph")
    assert r.similar[0]["incident"] == story["checkout"]
    assert all(s["parts"]["vector"] == 0 and s["basis"] == "structure" for s in r.similar)
    assert len(r.warnings) == 1 and "embeddings unavailable" in r.warnings[0]
    with pytest.raises(EmbeddingUnavailable):
        rc.recall_similar(g, DownEmbedder(), story["cur"], NOW, "vector")
    assert rc.index_incident(g, DownEmbedder(), story["cur"]) is False


def test_recency_breaks_ties_between_otherwise_identical_incidents(g):
    old = make_incident(g, "checkout-api errors", "checkout-api", [("checkout-api", "error_rate")], SIM_NOW - 90 * DAY)
    new = make_incident(g, "checkout-api errors", "checkout-api", [("checkout-api", "error_rate")], SIM_NOW)
    cur = make_incident(g, "checkout-api errors", "checkout-api", [("checkout-api", "error_rate")])
    r = rc.recall_similar(g, emb, cur, NOW, "graph")
    assert [s["incident"] for s in r.similar][:2] == [new, old]
    assert r.similar[0]["parts"]["recency"] > r.similar[1]["parts"]["recency"]


def test_cascading_incident_with_two_root_causes_appears_once_with_both_cause_types(g):
    ds = g.read("MATCH (d:Datastore {name:'payments-db'}) RETURN d.id")[0][0]
    casc = make_incident(g, "payments cascade", "checkout-api", [("checkout-api", "error_rate")], SIM_NOW)
    ep.confirm_root_cause(g, casc, "Datastore", ds, "investigator")
    cur = make_incident(g, "checkout-api errors", "checkout-api", [("checkout-api", "error_rate")])
    r = rc.recall_similar(g, emb, cur, NOW, "graph")
    hits = [s for s in r.similar if s["incident"] == casc]
    assert len(hits) == 1 and sorted(hits[0]["cause_types"]) == ["Datastore", "Deploy"]


def test_signature_text_contains_title_and_symptoms(g, story):
    text = rc.signature_text(ep.incident_view(g, story["cur"]))
    assert text.startswith("payment refunds are failing. symptoms:") and "error_rate on api-gateway" in text
