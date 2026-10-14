from collections import Counter

import pytest

from api.sim.faults import FAULT_CLASSES
from api.sim.generator import build_company
from api.sim.scenarios import build_scenarios
from api.sim.world import World


@pytest.fixture(scope="module")
def sets():
    return build_scenarios(build_company(42), 42)


def test_set_sizes(sets):
    assert len(sets["story"]) == 3 and len(sets["bench_32"]) == 32 and len(sets["heldout_8"]) == 8


def test_bench_is_8_classes_by_4_distinct_targets(sets):
    by_class = {}
    for s in sets["bench_32"]:
        by_class.setdefault(s["fault_class"], []).append(s["service"])
    assert set(by_class) == set(FAULT_CLASSES)
    assert all(len(v) == 4 and len(set(v)) == 4 for v in by_class.values())


def test_first_occurrence_of_each_class_is_novel(sets):
    novel = [s for s in sets["bench_32"] if s["novelty"] == "novel"]
    assert len(novel) == 8 and len({s["fault_class"] for s in novel}) == 8
    seen = set()
    for s in sets["bench_32"]:
        assert (s["novelty"] == "novel") == (s["fault_class"] not in seen)
        seen.add(s["fault_class"])


def test_heldout_targets_never_appear_in_story_or_bench(sets):
    held = {s["service"] for s in sets["heldout_8"]}
    assert len(held) == 8
    others = {s["service"] for s in sets["story"] + sets["bench_32"]}
    assert not held & others


def test_story_matches_the_demo_script(sets):
    assert [(s["fault_class"], s["service"]) for s in sets["story"]] == [
        ("bad_deploy", "checkout-api"), ("bad_deploy", "refund-api"), ("bad_flag", "fraud-scorer")]
    # refund-api never fails in the benchmark: incident 2 must be solved through structure
    assert "refund-api" not in {s["service"] for s in sets["bench_32"]}


def test_planted_injection_and_flaky_counts(sets):
    c = Counter()
    for s in sets["bench_32"]:
        c["planted"] += s["planted_injection"]
        c["flaky"] += s["self_heal"]
        assert not (s["planted_injection"] and s["self_heal"])
    assert c["planted"] == 3 and c["flaky"] == 1


def test_deterministic(sets):
    assert build_scenarios(build_company(42), 42) == sets


@pytest.mark.parametrize("name", ["story", "bench_32", "heldout_8"])
def test_every_scenario_can_be_injected_and_fixed(sets, name):
    w = World(build_company(42), 9)
    for s in sets[name]:
        w.inject(s["id"], s["fault_class"], s["service"], s["planted_injection"], s["self_heal"])
        assert w.get_alerts(s["id"]) or s["self_heal"]
        for st in w.truth(s["id"])["steps"]:
            assert w.act(s["id"], st["tool"], st["target"], st.get("value"))["outcome"] == "success"
        assert w.is_healthy(s["id"])
