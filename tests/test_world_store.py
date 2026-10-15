import json

import pytest

from api.sim import store
from api.sim.generator import build_company
from api.sim.world import World


@pytest.fixture
def company():
    return build_company(42)


def busy_world(company):
    w = World(company, 3)
    w.inject("a", "bad_flag", "checkout-api")
    w.inject("b", "db_pool_exhaustion", "payments-db", planted_injection=True)
    w.touch(); w.touch()
    top = w.truth("b")["extra"]["top_caller"]
    w.act("b", "scale", "payments-db")
    w.act("a", "rollback", "login-api")           # a harmful action, so fault state is non-trivial
    w.advance(3_600_000)
    return w, top


def roundtrip(company, w):
    return World.restore(company, json.loads(json.dumps(w.snapshot())))   # through real JSON, as on disk


def test_a_restored_world_is_indistinguishable_from_the_original(company):
    w, top = busy_world(company)
    r = roundtrip(company, w)
    assert (r.tick, r.offset_ms, r.now_ms) == (w.tick, w.offset_ms, w.now_ms)
    assert r.flag_values == w.flag_values and r.latest_deploy == w.latest_deploy
    for iid in ("a", "b"):
        assert r.truth(iid) == w.truth(iid)
        assert r.get_alerts(iid) == w.get_alerts(iid)
        assert r.is_healthy(iid) == w.is_healthy(iid)
    assert r.get_logs("b", "checkout-api") == w.get_logs("b", "checkout-api")      # incl. the planted line
    assert r.faults["b"].herrings == w.faults["b"].herrings and isinstance(r.faults["b"].herrings[0], tuple)


def test_a_restored_world_keeps_playing_the_same_game(company):
    w, top = busy_world(company)
    r = roundtrip(company, w)
    assert w.act("b", "restart", top) == r.act("b", "restart", top)                # finishes the db_pool fix
    assert w.is_healthy("b") and r.is_healthy("b")
    # randomness continues from the same point: the next injection mints the same ids and events
    assert w.inject("c", "bad_deploy", "refund-api") == r.inject("c", "bad_deploy", "refund-api")
    assert w.get_alerts("c") == r.get_alerts("c")


def test_snapshot_is_plain_json_and_does_not_mutate_the_world(company):
    w, _ = busy_world(company)
    before = w.get_alerts("a")
    blob = json.dumps(w.snapshot())
    assert json.loads(blob)["faults"]["a"]["fault_class"] == "bad_flag"
    assert w.get_alerts("a") == before


def test_store_saves_atomically_loads_and_deletes(company, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    assert store.load("t1", company) is None
    w, _ = busy_world(company)
    store.save("t1", w)
    assert [p.name for p in tmp_path.iterdir()] == ["t1.json"]                     # no stray temp file
    again = store.load("t1", company)
    assert again.truth("b") == w.truth("b")
    store.save("t1", World(company, 3))                                            # overwrite
    assert store.load("t1", company).faults == {}
    store.delete("t1")
    store.delete("t1")                                                              # idempotent
    assert store.load("t1", company) is None
