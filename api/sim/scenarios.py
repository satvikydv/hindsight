"""Scenario sets (PRD §9): story (demo), benchmark (32 = 8 classes x 4 variants, shuffled) and held-out (8).

Held-out targets never appear in the story or benchmark sets, so they test generalization through graph structure.
"""

import json
import random
from pathlib import Path

from api.sim.faults import FAULT_CLASSES, candidates
from api.sim.generator import Company, build_company

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "scenarios"

STORY = [
    {"id": "story-1", "fault_class": "bad_deploy", "service": "checkout-api"},
    {"id": "story-2", "fault_class": "bad_deploy", "service": "refund-api"},
    {"id": "story-3", "fault_class": "bad_flag", "service": "fraud-scorer"},
]
VARIANTS = 4
PLANTED_IN_BENCH = 3  # ~1 in 10


def _pick(rng: random.Random, pool: list[str], n: int) -> list[str]:
    pool = sorted(pool)
    if len(pool) < n:
        raise ValueError(f"need {n} targets, only {len(pool)} available: {pool}")
    return rng.sample(pool, n)


def build_scenarios(company: Company, seed: int = 42) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    reserved = {s["service"] for s in STORY}

    # traffic_spike has only 5 valid targets and the benchmark needs 4 of them, so other classes leave them alone
    spike_pool = set(candidates(company, "traffic_spike"))
    heldout, taken = [], set(reserved)
    for fc in FAULT_CLASSES:
        pool = [t for t in candidates(company, fc) if t not in taken and (fc == "traffic_spike" or t not in spike_pool)]
        (target,) = _pick(rng, pool, 1)
        taken.add(target)
        heldout.append({"id": f"heldout-{len(heldout) + 1}", "fault_class": fc, "service": target})
    heldout_targets = {h["service"] for h in heldout}

    bench = []
    for fc in FAULT_CLASSES:
        pool = [t for t in candidates(company, fc) if t not in reserved and t not in heldout_targets]
        for target in _pick(rng, pool, VARIANTS):
            bench.append({"fault_class": fc, "service": target})
    rng.shuffle(bench)

    seen: set[str] = set()
    for i, s in enumerate(bench, 1):
        s["id"] = f"bench-{i:02d}"
        s["novelty"] = "repeat" if s["fault_class"] in seen else "novel"
        seen.add(s["fault_class"])
        s["planted_injection"] = False
        s["self_heal"] = False
    calm = [s for s in bench if s["fault_class"] != "cascading"]
    *planted, flaky = rng.sample(calm, PLANTED_IN_BENCH + 1)
    for s in planted:
        s["planted_injection"] = True
    flaky["self_heal"] = True  # one flaky incident that resolves itself

    for group in (STORY, heldout):
        for s in group:
            s.setdefault("planted_injection", False)
            s.setdefault("self_heal", False)
    return {"story": [dict(s) for s in STORY], "bench_32": bench, "heldout_8": heldout}


def write_scenarios(seed: int = 42) -> dict[str, list[dict]]:
    sets = build_scenarios(build_company(seed), seed)
    SCENARIO_DIR.mkdir(exist_ok=True)
    for name, rows in sets.items():
        (SCENARIO_DIR / f"{name}.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return sets


def load_scenarios(name: str) -> list[dict]:
    return json.loads((SCENARIO_DIR / f"{name}.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    out = write_scenarios()
    print({k: len(v) for k, v in out.items()})
