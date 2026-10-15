import json

import pytest

from api import config
from api.bench import metrics, runner
from api.bench.runner import BenchConfig, estimate_cost, prove_graph_is_required, run_benchmark
from api.bench.scripted import expert_llm
from api.graph import client
from api.llm import FakeEmbedder, ScriptedLLM


def rec(mode, seed=1, index=1, set_="bench_32", novelty="repeat", steps=6, tokens=1000, cost=0.01, resolved=True,
        harmful=0, fast_path=False, confirmed=True, cause_correct=True, recalled=None, planted=False, self_heal=False,
        declined=False, fault_class="bad_deploy"):
    return dict(mode=mode, seed=seed, set=set_, index=index, scenario_id=f"x-{index}", fault_class=fault_class,
                service="s", novelty=novelty, planted_injection=planted, self_heal=self_heal, incident_id="i",
                status="resolved" if resolved else "failed", reason="", resolved=resolved, steps=steps, tokens=tokens,
                cost_usd=cost, harmful=harmful, fast_path=fast_path, declined=declined, confirmed=confirmed,
                cause_correct=cause_correct if confirmed else None, recalled_same_class=recalled, handoffs=2,
                wall_ms=1)


# ---- metrics: pure math -------------------------------------------------------------------------------------------
def synthetic():
    off = [rec("off", index=1, novelty="novel", steps=10, tokens=2000), rec("off", index=2, steps=10, tokens=2000),
           rec("off", index=3, steps=10, tokens=2000, resolved=False),
           rec("off", index=4, set_="heldout_8", novelty="heldout", steps=10)]
    graph = [rec("graph", index=1, novelty="novel", steps=10, tokens=2000, recalled=None),
             rec("graph", index=2, steps=4, tokens=1000, fast_path=True, confirmed=False, recalled=True),
             rec("graph", index=3, steps=6, tokens=1400, recalled=False, cause_correct=False),
             rec("graph", index=4, set_="heldout_8", novelty="heldout", steps=5, harmful=0)]
    return off + graph


def test_aggregate_compares_graph_with_off_using_only_what_each_metric_should_use():
    a = metrics.aggregate(synthetic())
    off, graph = a["modes"]["off"], a["modes"]["graph"]
    assert off["resolution_rate"] == pytest.approx(2 / 3, abs=1e-3) and graph["resolution_rate"] == 1.0
    assert (off["steps_repeat"], graph["steps_repeat"]) == (10.0, 5.0)         # repeat = index 2 and 3
    assert (off["tokens_repeat"], graph["tokens_repeat"]) == (2000.0, 1200.0)
    assert graph["cause_accuracy"] == 0.5                                       # only confirmed incidents are scored
    assert graph["confirmed_rate"] == pytest.approx(2 / 3, abs=1e-3)            # main set only: held-out is separate
    assert graph["recall_at_3"] == 0.5 and graph["recall_eligible"] == 2       # None (not applicable) is excluded
    assert graph["fast_path_rate_repeat"] == 0.5
    c = a["comparisons"]["graph"]
    assert c["resolution_rate_points"] == pytest.approx(33.33, abs=0.01)
    assert c["steps_reduction_repeat_pct"] == 50.0 and c["tokens_reduction_repeat_pct"] == 40.0
    assert c["heldout_steps_reduction_pct"] == 50.0


def test_self_healing_blips_do_not_distort_step_counts_and_injection_is_counted_separately():
    rows = [rec("off", index=1, steps=10), rec("off", index=2, steps=2, self_heal=True),
            rec("off", index=3, steps=10, planted=True, harmful=1), rec("off", index=4, steps=10, planted=True)]
    off = metrics.aggregate(rows)["modes"]["off"]
    assert off["steps_mean"] == 10.0                                           # the 2-step self-heal is ignored
    assert (off["injection_harmful"], off["injection_cases"], off["harmful_total"]) == (1, 2, 1)


def test_targets_report_met_or_not_without_pretending():
    a = metrics.aggregate(synthetic())
    t = a["targets"]
    assert t["steps_reduction_repeat_pct"]["met"] is True and t["tokens_reduction_repeat_pct"]["met"] is True
    assert t["resolution_rate_points"]["met"] is True and t["harmful_total"]["met"] is True
    assert t["cause_accuracy"]["met"] is False and t["recall_at_3"]["met"] is False        # 0.5 < 0.85 / 0.8
    flat = metrics.aggregate([rec("off"), rec("graph")])["targets"]
    assert flat["resolution_rate_points"]["measured"] == 0.0 and flat["resolution_rate_points"]["met"] is False


def test_modes_without_a_baseline_have_no_comparisons():
    a = metrics.aggregate([rec("graph")])
    assert a["comparisons"] == {} and "targets" not in a


def test_learning_curve_is_mean_with_min_max_across_seeds_and_skips_self_heals():
    rows = [rec("graph", seed=1, index=1, steps=10), rec("graph", seed=2, index=1, steps=14),
            rec("graph", seed=1, index=2, steps=6), rec("graph", seed=2, index=2, steps=4),
            rec("graph", seed=1, index=3, steps=2, self_heal=True)]
    assert metrics.learning_curve(rows, "graph") == [{"index": 1, "mean": 12.0, "min": 10, "max": 14, "n": 2},
                                                     {"index": 2, "mean": 5.0, "min": 4, "max": 6, "n": 2}]


def result_for(records, llm="scripted", partial=False):
    return {"bench_id": "t", "config": {"llm": llm, "modes": ["off", "graph"], "seeds": 1, "sets": ["bench_32"],
                                        "limit": None}, "records": records, "total_cost_usd": 0.1,
            "partial": partial, "stopped_reason": "cost cap" if partial else None,
            "aggregate": metrics.aggregate(records), "curves": metrics.curves(records)}


def test_markdown_labels_scripted_runs_partial_runs_and_misses_honestly(tmp_path):
    md = metrics.render_markdown(result_for(synthetic(), partial=True))
    assert "Scripted policy, not the language model" in md and "Partial run" in md
    assert "## PRD targets" in md and "**no**" in md and "## Learning curve" in md
    assert "nominal" in md and "nothing spent" in md and "does not read" in md      # scripted runs say what they are not
    assert "| resolution rate | 67% | 100% |" in md                                  # rates print as percentages
    real = metrics.render_markdown(result_for(synthetic(), llm="openai"))
    assert "Scripted policy" not in real and "Partial run" not in real and "nominal" not in real
    assert "total LLM cost" in real
    j, m = metrics.save(result_for(synthetic()), tmp_path)
    assert json.loads(j.read_text())["bench_id"] == "t" and m.read_text().startswith("# Benchmark t")


# ---- the runner, offline, against the real graph ------------------------------------------------------------------
def scripted_run(**kw):
    cfg = BenchConfig(**{"workers": 3, **kw})
    return run_benchmark(cfg, expert_llm, lambda: FakeEmbedder(64))


@pytest.fixture(scope="module")
def full():
    return scripted_run(seeds=2)


def test_full_run_covers_every_mode_seed_and_incident_with_no_errors(full):
    assert len(full["records"]) == 3 * 2 * 40
    assert not [r for r in full["records"] if r["status"] == "error"], \
        [r["reason"] for r in full["records"] if r["status"] == "error"][:3]
    assert {r["mode"] for r in full["records"]} == {"off", "vector", "graph"} and not full["partial"]


def test_every_incident_resolves_with_zero_harm_including_injection_and_heldout(full):
    bad = [(r["mode"], r["scenario_id"], r["status"], r["harmful"]) for r in full["records"]
           if not r["resolved"] or r["harmful"]]
    assert bad == []
    assert full["aggregate"]["modes"]["graph"]["injection_cases"] == 3 * 2        # 3 planted scenarios x 2 seeds
    assert all(m["injection_harmful"] == 0 for m in full["aggregate"]["modes"].values())


def test_memory_mechanics_show_up_in_the_numbers(full):
    a = full["aggregate"]
    off, vec, graph = a["modes"]["off"], a["modes"]["vector"], a["modes"]["graph"]
    assert off["fast_path_rate_repeat"] == 0 and vec["fast_path_rate_repeat"] == 0     # only graph memory has playbooks
    assert graph["fast_path_rate_repeat"] > 0.7
    assert graph["steps_repeat"] < off["steps_repeat"] * 0.85                           # fast path saves steps
    assert graph["tokens_repeat"] < off["tokens_repeat"] and graph["cost_mean_usd"] < off["cost_mean_usd"]
    assert abs(graph["steps_novel"] - off["steps_novel"]) <= 1.0                        # nothing to remember on the first of a class
    assert a["comparisons"]["graph"]["steps_reduction_repeat_pct"] > 15
    assert graph["heldout_steps"] < off["heldout_steps"]                                # transfers to never-failed services


def test_the_first_incident_of_every_class_is_investigated_cold_in_graph_mode(full):
    novel = [r for r in full["records"] if r["mode"] == "graph" and r["set"] == "bench_32" and r["novelty"] == "novel"]
    assert len(novel) == 2 * 8 and not any(r["fast_path"] for r in novel)


def test_recall_and_root_cause_scoring(full):
    graph, off = full["aggregate"]["modes"]["graph"], full["aggregate"]["modes"]["off"]
    assert graph["recall_at_3"] is not None and graph["recall_at_3"] >= 0.8 and graph["recall_eligible"] > 10
    assert off["recall_at_3"] is None                                                   # no recall in off mode
    assert graph["cause_accuracy"] == 1.0 and off["cause_accuracy"] == 1.0              # what was confirmed was right
    assert off["confirmed_rate"] == 1.0 and graph["confirmed_rate"] < off["confirmed_rate"]   # fast path never confirms


def test_curves_exist_per_mode_and_graph_trends_below_off(full):
    cv = full["curves"]
    assert set(cv) == {"off", "vector", "graph"} and all(len(c) == 31 for c in cv.values())   # self-heal index skipped
    late = lambda m: sum(p["mean"] for p in cv[m][-12:]) / 12
    assert late("graph") < late("off")


def test_tenants_are_cleaned_up_and_modes_did_not_share_graphs(full):
    assert not [g for g in client.get_db().list_graphs() if "bench" in g]


def test_runs_are_reproducible_with_the_scripted_policy():
    key = lambda r: (r["mode"], r["scenario_id"], r["status"], r["steps"], r["tokens"], r["fast_path"], r["harmful"])
    a = scripted_run(seeds=1, modes=("graph",), sets=("story",))
    b = scripted_run(seeds=1, modes=("graph",), sets=("story",))
    assert [key(r) for r in a["records"]] == [key(r) for r in b["records"]] and len(a["records"]) == 3


def test_limit_makes_a_cheap_pilot():
    r = scripted_run(seeds=1, modes=("graph",), limit=4)
    assert [x["index"] for x in r["records"] if x["set"] == "bench_32"] == [1, 2, 3, 4]
    assert len([x for x in r["records"] if x["set"] == "heldout_8"]) == 4


def test_a_cost_cap_stops_the_run_cleanly_and_says_so():
    r = scripted_run(seeds=1, modes=("off",), max_cost_usd=0.06, workers=1)
    assert r["partial"] is True and "cost cap" in r["stopped_reason"]
    assert 0 < len(r["records"]) < 40 and r["total_cost_usd"] >= 0.06
    assert r["total_cost_usd"] < 0.06 + 0.15                                            # overshoots by at most one incident
    assert not [g for g in client.get_db().list_graphs() if "bench" in g]


def test_a_crashing_model_is_recorded_per_incident_not_fatal():
    def boom(tier, messages, tools):
        raise RuntimeError("model client bug")

    r = run_benchmark(BenchConfig(seeds=1, modes=("graph",), sets=("bench_32",), limit=3, workers=1),
                      lambda: ScriptedLLM(boom), lambda: FakeEmbedder(64))
    assert len(r["records"]) == 3 and all(x["status"] == "error" for x in r["records"])
    assert "RuntimeError" in r["records"][0]["reason"] and r["aggregate"]["modes"]["graph"]["resolution_rate"] == 0.0
    assert not [g for g in client.get_db().list_graphs() if "bench" in g]


# ---- cost guard ---------------------------------------------------------------------------------------------------
def test_estimate_scales_with_scope():
    full = estimate_cost(BenchConfig())
    assert full["incidents_total"] == 360 and full["low_usd"] < full["high_usd"]
    pilot = estimate_cost(BenchConfig(seeds=1, modes=("graph",), limit=4))
    assert pilot["incidents_total"] == 8 and pilot["high_usd"] < 0.5 < full["low_usd"]


def test_the_cli_refuses_a_real_model_run_without_explicit_approval(monkeypatch, capsys):
    import api.llm as llm_module

    def forbidden(*a, **k):
        raise AssertionError("a real OpenAI client was constructed without --yes-spend")

    monkeypatch.setattr(llm_module, "OpenAILLM", forbidden)
    monkeypatch.setattr(llm_module, "OpenAIEmbedder", forbidden)
    assert runner.main(["--llm", "openai", "--seeds", "1", "--modes", "graph", "--limit", "2"]) == 2
    out = capsys.readouterr().out
    assert "estimated $" in out and "--yes-spend" in out and "Not running" in out
    assert runner.main(["--llm", "openai", "--estimate"]) == 0                           # estimate only: still free
    assert runner.main(["--estimate"]) == 0 and "free" in capsys.readouterr().out


def test_the_cli_runs_scripted_and_saves_both_files(tmp_path, capsys):
    code = runner.main(["--modes", "graph", "--seeds", "1", "--sets", "story", "--out", str(tmp_path)])
    assert code == 0 and capsys.readouterr().out.count("graph  ") >= 3
    names = sorted(p.name for p in tmp_path.iterdir())
    assert len(names) == 2 and names[0].endswith(".json") and names[1].endswith(".md") and "scripted" in names[0]


# ---- rule 3 -------------------------------------------------------------------------------------------------------
def test_without_falkordb_incidents_fail_and_the_real_connection_is_restored():
    before = client._db
    r = prove_graph_is_required()
    assert r["incident_failed"] is True and r["graph_reachable"] is False
    assert "ConnectionError" in r["error"]
    assert client._db is before and client.ping()
