import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from ulid import ULID

from api import config
from api.graph.client import TenantGraph
from api.llm import FakeEmbedder
from api.main import create_app
from api.memory import episodic as ep
from api.runtime import Runtime
from api.sim import store
from api.sim.generator import SIM_NOW, seed_tenant
from api.tenancy import create_tenant, delete_tenant, resolve_key
from api.tools import scribe_tools
from tests.scripted import scripted
from tests.test_coordinator import Env, std_plans


class ApiEnv(Env):
    """Plans need the incident's graph and simulator; in the API the incident id arrives from the context message."""

    def __init__(self):
        super().__init__(None)
        self.rt = None

    def bind(self, iid):
        self.iid = iid
        tr = self.rt.tenant(self.rt.runs[iid].tenant_id)
        self.rig = SimpleNamespace(g=tr.graph, w=tr.world)


class Gate:
    """Holds every LLM call until released, to keep an incident 'running' while the test pokes at it."""

    def __init__(self, inner):
        self.inner, self.event = inner, threading.Event()

    def chat(self, tier, messages, tools):
        self.event.wait(15)
        return self.inner.chat(tier, messages, tools)


def parse_sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        d = dict(line.partition(": ")[::2] for line in block.splitlines())
        out.append((int(d["id"]), d["event"], json.loads(d["data"])))
    return out


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EMBED_DIM", 64)
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "sim")
    monkeypatch.setattr(scribe_tools, "DATA_DIR", tmp_path / "pm")
    env = ApiEnv()
    gate = Gate(scripted(std_plans(), env))
    gate.event.set()                                    # open by default; tests that need it closed clear it
    app = create_app(llm=gate, embedder=FakeEmbedder(64), resume=False, demo=False)
    env.rt = app.state.runtime
    created = []
    with TestClient(app) as c:
        def tenant(name="apitest", seed=42):
            r = c.post("/tenants", json={"name": name, "seed": seed})
            assert r.status_code == 200, r.text
            created.append(r.json()["tenant_id"])
            return r.json()["tenant_id"], {"X-Tenant-Key": r.json()["api_key"]}

        def run(headers, body=None):
            r = c.post("/incidents", json=body or {}, headers=headers)
            assert r.status_code == 202, r.text
            iid = r.json()["incident_id"]
            assert app.state.runtime.runs[iid].done.wait(40), "incident did not finish"
            return iid

        yield SimpleNamespace(c=c, app=app, rt=app.state.runtime, tenant=tenant, run=run, gate=gate, env=env)
    for tid in created:
        delete_tenant(tid)


# ---- service and auth ---------------------------------------------------------------------------------------------
def test_health_and_tenant_creation(api):
    h = api.c.get("/health").json()
    assert h["falkordb"] == "up" and "version" in h
    tid, hdr = api.tenant()
    t = api.c.get("/tenant", headers=hdr).json()
    assert t["tenant_id"] == tid and t["services"] == 40


@pytest.mark.parametrize("method,path", [
    ("get", "/tenant"), ("get", "/incidents"), ("post", "/incidents"), ("get", "/incidents/x"),
    ("get", "/incidents/x/trace"), ("get", "/incidents/x/stream"), ("post", "/incidents/x/cancel"),
    ("get", "/actions/x/explain"), ("get", "/graph/subgraph?center=x"), ("get", "/playbooks"),
    ("post", "/facts/correct"), ("post", "/demo/reset")])
def test_every_tenant_route_rejects_a_missing_or_bad_key(api, method, path):
    assert getattr(api.c, method)(path).status_code == 401
    assert getattr(api.c, method)(path, headers={"X-Tenant-Key": "hk_nope"}).status_code == 401


# ---- incidents ----------------------------------------------------------------------------------------------------
def test_default_incident_is_accepted_runs_and_resolves(api):
    _, hdr = api.tenant()
    r = api.c.post("/incidents", json={}, headers=hdr)
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["service"] == "checkout-api"
    iid = r.json()["incident_id"]
    assert api.rt.runs[iid].done.wait(40)
    inc = api.c.get(f"/incidents/{iid}", headers=hdr).json()
    assert inc["id"] == iid and inc["status"] == "resolved" and inc["running"] is False
    assert [a["agent"] for a in inc["actions"]].count("remediator") == 2
    assert [(h["from"], h["to"]) for h in inc["handoffs"]] == [("triage", "investigator"),
                                                               ("investigator", "remediator")]
    listed = api.c.get("/incidents", headers=hdr).json()["incidents"]
    row = next(i for i in listed if i["id"] == iid)
    assert (row["status"], row["steps_to_resolve"], row["fast_path"], row["mode"]) == ("resolved", 8, False, "graph")
    assert row["cost_to_resolve"] > 0


def test_second_incident_takes_the_fast_path_over_the_api(api):
    _, hdr = api.tenant()
    api.run(hdr)
    iid = api.run(hdr, {"fault_class": "bad_deploy", "service": "refund-api"})
    rows = {i["id"]: i for i in api.c.get("/incidents", headers=hdr).json()["incidents"]}
    assert rows[iid]["fast_path"] is True and rows[iid]["steps_to_resolve"] == 5
    assert api.c.get("/playbooks", headers=hdr).json()["playbooks"][0]["attempts"] == 2


def test_validation_scenarios_and_modes(api):
    _, hdr = api.tenant()
    bad = api.c.post("/incidents", json={"fault_class": "nonsense"}, headers=hdr)
    assert bad.status_code == 400 and "bad_deploy" in bad.json()["detail"]
    wrong = api.c.post("/incidents", json={"fault_class": "db_pool_exhaustion", "service": "checkout-api"}, headers=hdr)
    assert wrong.status_code == 400 and "payments-db" in wrong.json()["detail"]
    assert api.c.post("/incidents", json={"scenario_id": "story-99"}, headers=hdr).status_code == 404
    assert api.c.post("/incidents", json={"mode": "psychic"}, headers=hdr).status_code == 422
    ok = api.c.post("/incidents", json={"scenario_id": "story-1", "mode": "off"}, headers=hdr)
    assert ok.status_code == 202 and ok.json()["mode"] == "off" and ok.json()["fault_class"] == "bad_deploy"
    api.rt.runs[ok.json()["incident_id"]].done.wait(40)


def test_rate_limit_is_per_address_and_sets_retry_after(monkeypatch, api):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_HOUR", 2)
    app = create_app(llm=api.gate, embedder=FakeEmbedder(64), resume=False, demo=False)
    api.env.rt = app.state.runtime
    with TestClient(app) as c:
        r = c.post("/tenants", json={"name": "rl"})
        tid, hdr = r.json()["tenant_id"], {"X-Tenant-Key": r.json()["api_key"]}
        try:
            codes = [c.post("/incidents", json={}, headers=hdr).status_code for _ in range(3)]
            assert codes == [202, 202, 429]
            limited = c.post("/incidents", json={}, headers=hdr)
            assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
            other = c.post("/incidents", json={}, headers={**hdr, "X-Forwarded-For": "203.0.113.9"})
            assert other.status_code == 202                      # a different address has its own budget
            for run in list(app.state.runtime.runs.values()):
                run.done.wait(40)
        finally:
            delete_tenant(tid)


def test_without_an_llm_key_incidents_are_refused_but_the_rest_works(monkeypatch, api):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    app = create_app(resume=False, demo=False)                  # no injected llm, no key
    with TestClient(app) as c:
        r = c.post("/tenants", json={"name": "nokey"})
        tid, hdr = r.json()["tenant_id"], {"X-Tenant-Key": r.json()["api_key"]}
        try:
            assert c.get("/health").json()["llm_key_present"] is False
            refused = c.post("/incidents", json={}, headers=hdr)
            assert refused.status_code == 503 and "LLM_API_KEY" in refused.json()["detail"]
            assert c.get("/playbooks", headers=hdr).status_code == 200
        finally:
            delete_tenant(tid)


def test_a_crashing_worker_is_reported_not_swallowed(api):
    class Boom:
        def chat(self, *a):
            raise RuntimeError("bug in the model client")

    api.rt._llm = Boom()
    _, hdr = api.tenant()
    iid = api.run(hdr)
    run = api.rt.runs[iid]
    assert run.status == "error"
    assert [e for _, e, _ in run.events][-2:] == ["failed", "stream_end"]
    assert run.events[-2][2]["reason"] == "internal_error"


# ---- streaming ----------------------------------------------------------------------------------------------------
def stream(api, iid, hdr=None, params="", extra=None):
    with api.c.stream("GET", f"/incidents/{iid}/stream{params}", headers={**(hdr or {}), **(extra or {})}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        return parse_sse("".join(r.iter_text()))


def test_stream_tells_the_whole_story_in_order_with_sequential_ids(api):
    _, hdr = api.tenant()
    iid = api.run(hdr)
    events = stream(api, iid, hdr)
    names = [e for _, e, _ in events]
    assert [i for i, _, _ in events] == list(range(1, len(events) + 1))
    assert names[-1] == "stream_end" and names.count("handoff") == 2 and "resolved" in names
    assert names.index("agent_started") < names.index("tool_called") < names.index("tool_result")
    assert names.index("resolved") < [i for i, n in enumerate(names) if n == "agent_started"][-1]   # Scribe is last
    tools = [d["tool"] for _, e, d in events if e == "tool_called"]
    assert tools[:3] == ["recall_similar", "blast_radius", "set_severity"] and "rollback" in tools


def test_stream_can_be_followed_live_while_the_incident_runs(api):
    _, hdr = api.tenant()
    api.gate.event.clear()
    iid = api.c.post("/incidents", json={}, headers=hdr).json()["incident_id"]
    threading.Timer(0.4, api.gate.event.set).start()           # the incident only gets going after we are listening
    events = stream(api, iid, hdr)
    assert events[-1][1] == "stream_end" and any(e == "resolved" for _, e, _ in events)


def test_stream_resumes_after_last_event_id(api):
    _, hdr = api.tenant()
    iid = api.run(hdr)
    full = stream(api, iid, hdr)
    rest = stream(api, iid, hdr, extra={"Last-Event-ID": "5"})
    assert [i for i, _, _ in rest] == [i for i, _, _ in full if i > 5]
    assert stream(api, iid, hdr, extra={"Last-Event-ID": str(len(full))}) == []


def test_browser_style_stream_auth_via_query_string_works_and_bad_key_does_not(api):
    _, hdr = api.tenant()
    iid = api.run(hdr)
    key = hdr["X-Tenant-Key"]
    assert stream(api, iid, params=f"?tenant_key={key}")[-1][1] == "stream_end"
    assert api.c.get(f"/incidents/{iid}/stream?tenant_key=wrong").status_code == 401
    assert api.c.get(f"/incidents/{iid}", params={"tenant_key": key}).status_code == 401   # only the stream route


def test_after_a_restart_the_stream_is_rebuilt_from_the_graph(api):
    _, hdr = api.tenant()
    iid = api.run(hdr)
    live = stream(api, iid, hdr)
    api.rt.runs.clear()                                         # the in-memory event log is gone
    rebuilt = stream(api, iid, hdr)
    assert [e for _, e, _ in rebuilt][-2:] == ["resolved", "stream_end"]
    live_tools = [d["tool"] for _, e, d in live if e == "tool_called"]
    assert [d["tool"] for _, e, d in rebuilt if e == "tool_called"] == live_tools
    assert [(d["from"], d["to"]) for _, e, d in rebuilt if e == "handoff"] == [("triage", "investigator"),
                                                                              ("investigator", "remediator")]
    assert [i for i, _, _ in stream(api, iid, hdr, extra={"Last-Event-ID": "4"})][0] == 5


def test_streaming_an_unknown_incident_is_404(api):
    _, hdr = api.tenant()
    assert api.c.get("/incidents/nope/stream", headers=hdr).status_code == 404


# ---- queueing and cancel ------------------------------------------------------------------------------------------
def test_one_incident_at_a_time_per_tenant_and_queued_ones_can_be_cancelled(api):
    _, hdr = api.tenant()
    api.gate.event.clear()
    first = api.c.post("/incidents", json={}, headers=hdr).json()["incident_id"]
    second = api.c.post("/incidents", json={"fault_class": "bad_deploy", "service": "refund-api"},
                        headers=hdr).json()["incident_id"]
    deadline = time.time() + 10
    while api.rt.runs[first].status != "running" and time.time() < deadline:
        time.sleep(0.05)
    assert api.rt.runs[second].status == "queued"
    q = api.c.get(f"/incidents/{second}", headers=hdr).json()
    assert q == {"id": second, "status": "queued", "mode": "graph", "queued": True}
    listed = api.c.get("/incidents", headers=hdr).json()["incidents"]
    assert {i["id"]: i["status"] for i in listed if i["id"] in (first, second)}[second] == "queued"
    assert api.c.post(f"/incidents/{second}/cancel", headers=hdr).status_code == 202
    api.gate.event.set()
    assert api.rt.runs[first].done.wait(40) and api.rt.runs[second].done.wait(40)
    assert api.rt.runs[first].status == "resolved"
    assert api.c.get(f"/incidents/{second}", headers=hdr).json()["reason"] == "cancelled"


def test_cancelling_a_running_incident_ends_it_failed_with_the_scribe_run(api):
    _, hdr = api.tenant()
    api.gate.event.clear()
    iid = api.c.post("/incidents", json={}, headers=hdr).json()["incident_id"]
    deadline = time.time() + 10
    while not (api.rt.runs[iid].coordinator and api.rt.runs[iid].status == "running") and time.time() < deadline:
        time.sleep(0.05)
    r = api.c.post(f"/incidents/{iid}/cancel", headers=hdr)
    assert r.status_code == 202 and r.json()["status"] == "cancelling"
    api.gate.event.set()
    assert api.rt.runs[iid].done.wait(40)
    inc = api.c.get(f"/incidents/{iid}", headers=hdr).json()
    assert (inc["status"], inc["reason"]) == ("failed", "cancelled")
    assert "consolidate" in [a["tool"] for a in inc["actions"]]
    assert api.c.post(f"/incidents/{iid}/cancel", headers=hdr).status_code == 409     # already over
    assert api.c.post("/incidents/nope/cancel", headers=hdr).status_code == 404


# ---- traceability and knowledge ----------------------------------------------------------------------------------
def test_explain_and_trace_routes(api):
    _, hdr = api.tenant()
    iid = api.run(hdr)
    rollback = next(a for a in api.c.get(f"/incidents/{iid}", headers=hdr).json()["actions"] if a["tool"] == "rollback")
    ex = api.c.get(f"/actions/{rollback['id']}/explain", headers=hdr).json()
    assert ex["action"]["tool"] == "rollback" and ex["reasoning"]["model"] and ex["evidence"][0]["name"] == "checkout-api"
    assert {"nodes", "edges"} <= set(ex["subgraph"]) and ex["highlight"]
    assert api.c.get("/actions/nope/explain", headers=hdr).status_code == 404
    tr = api.c.get(f"/incidents/{iid}/trace", headers=hdr).json()
    assert tr["cost"]["llm_calls"] == 10 and tr["timeline"][0]["tool"] == "recall_similar"
    md = api.c.get(f"/incidents/{iid}/trace", params={"format": "md"}, headers=hdr)
    assert md.headers["content-type"].startswith("text/markdown") and md.text.startswith("# Incident trace")
    assert api.c.get("/incidents/nope/trace", headers=hdr).status_code == 404


def test_subgraph_is_capped_readable_and_time_travels(api):
    tid, hdr = api.tenant()
    api.run(hdr)
    sg = api.c.get("/graph/subgraph", params={"center": "checkout-api", "hops": 1}, headers=hdr).json()
    names = {n["name"] for n in sg["nodes"]}
    assert "checkout-api" in names and "payment-orchestrator" in names and "Checkout" in names
    assert not {n["label"] for n in sg["nodes"]} & {"Message", "Action", "Symptom", "Agent"}
    ids = {n["id"] for n in sg["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in sg["edges"]) and sg["truncated"] == 0
    wide = api.c.get("/graph/subgraph", params={"center": "checkout-api", "hops": 2}, headers=hdr).json()
    assert len(wide["nodes"]) <= 300 and len(wide["nodes"]) > len(sg["nodes"])
    assert api.c.get("/graph/subgraph", params={"center": "checkout-api", "hops": 3}, headers=hdr).status_code == 422
    assert api.c.get("/graph/subgraph", params={"center": "no-such-node"}, headers=hdr).status_code == 404
    # ownership change: what we believed before vs after
    now = api.rt.tenant(tid).world.now_ms
    assert api.c.post("/facts/correct", json={"subject": "checkout-api", "predicate": "owner", "value": "Ledger",
                                              "reason": "reorg"}, headers=hdr).json()["status"] == "superseded"

    def owners(as_of):
        d = api.c.get("/graph/subgraph", params={"center": "checkout-api", "as_of": as_of}, headers=hdr).json()
        label = {n["id"]: n["name"] for n in d["nodes"]}
        return {label[e["from"]] for e in d["edges"] if e["type"] == "OWNS" and e["to"] == d["center"]}

    assert owners(now - 1) == {"Checkout"} and owners(now + 1) == {"Ledger"}


def test_fact_correction_errors_come_with_suggestions(api):
    _, hdr = api.tenant()
    miss = api.c.post("/facts/correct", json={"subject": "checkout-apy", "predicate": "owner", "value": "Ledger"},
                      headers=hdr)
    assert miss.status_code == 404 and "checkout-api" in miss.json()["suggestions"]
    team = api.c.post("/facts/correct", json={"subject": "checkout-api", "predicate": "owner", "value": "Ledgr"},
                      headers=hdr)
    assert team.status_code == 404 and "Ledger" in team.json()["suggestions"]
    assert api.c.post("/facts/correct", json={"subject": "", "predicate": "owner", "value": "x"},
                      headers=hdr).status_code == 422
    assert api.c.post("/facts/correct", json={"subject": "payments-db", "predicate": "owner", "value": "Ledger"},
                      headers=hdr).status_code == 400            # a datastore has no owner


def test_playbooks_route_lists_what_was_learned(api):
    _, hdr = api.tenant()
    assert api.c.get("/playbooks", headers=hdr).json() == {"playbooks": []}
    api.run(hdr)
    [pb] = api.c.get("/playbooks", headers=hdr).json()["playbooks"]
    assert pb["name"] == "bad_deploy: rollback" and pb["status"] == "active" and len(pb["derived_from"]) == 1


# ---- isolation ----------------------------------------------------------------------------------------------------
def test_tenants_cannot_see_stream_trace_explain_or_cancel_each_others_incidents(api):
    _, a = api.tenant("tenant a")
    _, b = api.tenant("tenant b", seed=7)
    iid = api.run(a)
    action = api.c.get(f"/incidents/{iid}", headers=a).json()["actions"][0]["id"]
    for path in (f"/incidents/{iid}", f"/incidents/{iid}/trace", f"/incidents/{iid}/stream",
                 f"/actions/{action}/explain"):
        assert api.c.get(path, headers=b).status_code == 404, path
    assert api.c.post(f"/incidents/{iid}/cancel", headers=b).status_code == 404
    assert api.c.get("/incidents", headers=b).json() == {"incidents": []}
    assert api.c.get("/playbooks", headers=b).json() == {"playbooks": []}


# ---- demo tenant --------------------------------------------------------------------------------------------------
def test_demo_tenant_is_seeded_on_boot_and_only_it_can_be_reset(monkeypatch, api):
    key = f"demo-{str(ULID()).lower()}"
    monkeypatch.setattr(config, "DEMO_TENANT_KEY", key)
    app = create_app(llm=api.gate, embedder=FakeEmbedder(64), resume=False, demo=True)
    api.env.rt = app.state.runtime
    demo_hdr = {"X-Tenant-Key": key}
    with TestClient(app) as c:
        try:
            first = c.get("/tenant", headers=demo_hdr).json()
            assert first["services"] == 40
            r = c.post("/incidents", json={}, headers=demo_hdr)
            app.state.runtime.runs[r.json()["incident_id"]].done.wait(40)
            assert len(c.get("/incidents", headers=demo_hdr).json()["incidents"]) == 1
            other = c.post("/tenants", json={"name": "not demo"}).json()
            try:
                assert c.post("/demo/reset", headers={"X-Tenant-Key": other["api_key"]}).status_code == 403
            finally:
                delete_tenant(other["tenant_id"])
            reset = c.post("/demo/reset", headers=demo_hdr)
            assert reset.status_code == 200 and reset.json()["tenant_id"] != first["tenant_id"]
            assert c.get("/incidents", headers=demo_hdr).json() == {"incidents": []}      # reseeded, history gone
            assert c.get("/tenant", headers=demo_hdr).json()["services"] == 40            # same key still works
            assert c.get("/playbooks", headers=demo_hdr).json() == {"playbooks": []}
        finally:
            tid = resolve_key(key)
            if tid:
                delete_tenant(tid)


# ---- restart ------------------------------------------------------------------------------------------------------
def runtime_for(plans, rt_env):
    rt = Runtime(lambda: scripted(plans, rt_env), lambda: FakeEmbedder(64))
    rt_env.rt = rt
    return rt


@pytest.fixture
def bare_tenant(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EMBED_DIM", 64)
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "sim")
    monkeypatch.setattr(scribe_tools, "DATA_DIR", tmp_path / "pm")
    tid, _ = create_tenant("restart")
    seed_tenant(TenantGraph(tid), 42)
    yield tid
    delete_tenant(tid)


def crash_plans():
    p = std_plans()
    p["investigator"] = [[("rank_suspects", {}), ("__crash__", {})],
                         [("propose_hypothesis", lambda e: {"text": "bad deploy", "score": 0.9,
                                                            "target_type": "Deploy", "target": e.cause}),
                          ("confirm_root_cause", lambda e: {"target_type": "Deploy", "target": e.cause})]]
    return p


def test_a_server_restart_resumes_the_active_incident_from_the_graph_and_the_saved_simulator(bare_tenant):
    tid = bare_tenant
    env1 = ApiEnv()
    rt1 = runtime_for(crash_plans(), env1)
    iid = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
    run = rt1.submit(tid, iid, {"fault_class": "bad_deploy", "service": "checkout-api"})
    assert run.done.wait(30) and run.status == "error"            # the "server" died mid-investigation
    g = TenantGraph(tid)
    assert ep.last_completed_action(g, iid)["tool"] == "rank_suspects"
    assert (store.STORE_DIR / f"{tid}.json").exists()
    g.write("MATCH (i:Incident {id:$i}) SET i.lock_until = 0", {"i": iid})              # its lock expires

    env2 = ApiEnv()
    plans2 = crash_plans()
    plans2["investigator"] = plans2["investigator"][1:]                                  # the new process does not crash
    rt2 = runtime_for(plans2, env2)                                                      # fresh process, no memory
    assert rt2.resume_all([tid]) == [iid]
    assert rt2.runs[iid].done.wait(30) and rt2.runs[iid].status == "resolved"
    view = ep.incident_view(g, iid)
    assert view["status"] == "resolved" and [a["tool"] for a in view["actions"]].count("recall_similar") == 1


def test_an_incident_whose_simulator_state_was_lost_is_failed_not_falsely_healed(bare_tenant):
    tid = bare_tenant
    rt1 = runtime_for(crash_plans(), ApiEnv())
    iid = "01HZZZZZZZZZZZZZZZZZZZZZZY"
    assert rt1.submit(tid, iid, {"fault_class": "bad_deploy", "service": "checkout-api"}).done.wait(30)
    g = TenantGraph(tid)
    g.write("MATCH (i:Incident {id:$i}) SET i.lock_until = 0", {"i": iid})
    store.delete(tid)                                                                    # the snapshot is gone
    rt2 = runtime_for(crash_plans(), ApiEnv())
    assert rt2.resume_all([tid]) == []
    view = ep.incident_view(g, iid)
    assert (view["status"], view["reason"]) == ("failed", "simulator_state_lost")
