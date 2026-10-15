"""The coordinator: a plain-Python state machine, not an LLM, so handoffs are predictable and explainable (PRD §7).

  OPEN -> TRIAGE -> INVESTIGATE -> REMEDIATE -> VERIFY -> RESOLVED -> CONSOLIDATE

- Fast path: if Triage's recall finds an active playbook with weight >= FAST_PATH_WEIGHT from a sufficiently similar past
  incident, skip INVESTIGATE. This is what makes incident #2 short.
- Bounce back: a failed remediation twice in a row (or a failed fast path) returns to INVESTIGATE with the hypotheses
  behind the failed actions marked rejected.
- Give up: 20 total tool calls or 10 minutes. The incident is marked failed, owners_of(origin) is recorded as the
  escalation target, and the Scribe still consolidates (failures teach too).
- Every transition writes a HANDOFF edge with a short summary. The receiving agent reads the graph, not the sender's prompt.

Resolution is decided by the simulator's health (the harness's ground truth), never by an agent's say-so.
"""

import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field

from api import config
from api.agents import prompts
from api.agents.base import LOCK_TTL_MS, SPECS, Emit, Seq, TurnResult, record_llm_call, run_turn
from api.agents.intake import open_from_page
from api.clock import wall_ms
from api.graph import queries as q
from api.graph.client import TenantGraph
from api.ids import new_id
from api.llm import LLM, Embedder, LLMUnavailable
from api.memory import episodic as ep
from api.memory import procedural as pr
from api.memory.consolidate import ACT_TOOLS
from api.sim.world import World, write_events
from api.tools import ToolContext, execute
from api.tools.scribe_tools import stub_postmortem

log = logging.getLogger(__name__)

MAX_STEPS = 20
MAX_SECONDS = 600
REMEDIATE_RETRIES = 2          # failed remediation turns before going back to investigate
SCRIBE_ACTIONS = ("consolidate", "link_people")


@dataclass
class IncidentResult:
    incident_id: str
    status: str                 # resolved | failed | active (paused: lock lost)
    reason: str
    steps: int                  # tool calls to resolve, before the Scribe
    tokens: int
    fast_path: bool = False
    handoffs: int = 0
    harmful_actions: int = 0    # benchmark scoring only: read from simulator ground truth
    cost_usd: float = 0.0       # LLM dollars spent to resolve (before the Scribe), from per-call usage
    report: dict = field(default_factory=dict)


class Coordinator:
    def __init__(self, g: TenantGraph, world: World, llm: LLM, embedder: Embedder | None = None,
                 mode: str = "graph", emit: Emit | None = None, max_steps: int = MAX_STEPS,
                 max_seconds: float = MAX_SECONDS):
        self.g, self.world, self.llm, self.embedder, self.mode = g, world, llm, embedder, mode
        self.emit = emit or (lambda event, data: None)
        self.max_steps, self.max_seconds = max_steps, max_seconds
        self._cancelled: set[str] = set()

    # ---- public ----------------------------------------------------------------------------------------------------
    def cancel(self, iid: str) -> None:
        """Stop after the current tool call; the incident ends failed with reason cancelled (PRD §13)."""
        self._cancelled.add(iid)

    def launch(self, fault_class: str, service: str | None = None, iid: str | None = None, **inject) -> str:
        """Inject a fault, page the crew and open the incident. Returns the incident id (use run() next). The API
        passes `iid` so it can hand the id to the client before the (possibly queued) incident actually starts."""
        iid = iid or new_id()
        write_events(self.g, self.world.inject(iid, fault_class, service, **inject))
        open_from_page(self.g, self.world, iid)
        return iid

    def run(self, iid: str) -> IncidentResult:
        """Drive the incident to the end. Safe to call again after a crash: it continues from the graph."""
        ctx = ToolContext(self.g, self.world, iid, self.mode, self.embedder)
        seq = Seq(ctx)
        view = ep.incident_view(self.g, iid)
        if view is None:
            raise LookupError(f"no incident {iid}")
        if view["status"] in ("resolved", "failed"):
            if not self._has_postmortem(iid):               # a crash between RESOLVED and CONSOLIDATE
                self._scribe(ctx, seq, view["status"], view.get("reason") or "")
            return self._result(ctx, view["status"], view.get("reason") or "", {})

        deadline = time.monotonic() + self.max_seconds
        agent = self._resume_agent(iid)
        hseq = len(ep.handoffs(self.g, iid))
        failures = bounce = 0
        fast_path = self._fast_path_of(iid)     # the playbook currently being followed; cleared when it fails
        used_fast = fast_path is not None       # whether this incident started on the fast path (for the results)
        try:
            while True:
                stop = self._stop_reason(ctx, deadline)
                if stop:
                    return self._finish(ctx, seq, "failed", stop, used_fast)
                if self.world.is_healthy(iid):
                    return self._finish(ctx, seq, "resolved", self._resolved_reason(iid), used_fast)

                self.emit("state_changed", {"incident": iid, "state": agent.upper()})
                if agent == "triage":
                    turn = self._triage(ctx, seq)
                    fast_path = self._pick_fast_path(ctx)
                    nxt = "remediator" if fast_path else "investigator"
                    if fast_path:
                        used_fast = True
                        ep.set_fast_path(self.g, iid, fast_path["playbook"])
                elif agent == "investigator":
                    turn = self._turn(ctx, seq, "investigator", self._bounce_note(bounce))
                    nxt = "remediator"
                else:
                    notes = self._fast_path_note(ctx, fast_path) if fast_path and failures == 0 and not bounce else (
                        "Your previous attempt did not restore health. Do not repeat actions that had no effect; "
                        "try a different one." if failures else "")
                    turn = self._turn(ctx, seq, "remediator", notes)
                    nxt = "remediator"

                if turn.reason == "cancelled":
                    return self._finish(ctx, seq, "failed", "cancelled", used_fast)
                if turn.reason == "token_cap":
                    return self._finish(ctx, seq, "failed", "token_cap", used_fast)
                if turn.reason == "lock_lost":
                    return self._result(ctx, "active", "lock_lost", {})

                if agent == "remediator":
                    if self.world.is_healthy(iid):
                        continue                                  # top of loop records the resolution
                    failures += 1
                    if fast_path and not any(o.name in ACT_TOOLS and o.outcome in ("success", "no_effect", "harmful")
                                             for o in turn.outcomes):
                        ep.decline_fast_path(self.g, iid)         # precondition check said no: never actually tried
                    self._reject_failed_hypotheses(ctx)
                    # a failed playbook goes straight back to investigating; otherwise allow one more attempt first
                    if fast_path or failures >= REMEDIATE_RETRIES:
                        nxt, fast_path = "investigator", None
                        bounce, failures = bounce + 1, 0
                if nxt != agent:
                    hseq += 1
                    self._handoff(ctx, agent, nxt, hseq, turn.summary or f"{agent} turn ended ({turn.reason})")
                agent = nxt
        except LLMUnavailable as e:
            log.error("incident %s: LLM unavailable: %s", iid, e)
            return self._finish(ctx, seq, "failed", "llm_unavailable", used_fast)

    # ---- phases ----------------------------------------------------------------------------------------------------
    def _turn(self, ctx: ToolContext, seq: Seq, agent: str, notes: str = "", steps_used: int = 0,
              announce: bool = True) -> TurnResult:
        spec = SPECS[agent]
        remaining = self.max_steps - self._steps(ctx)
        spec = dataclasses.replace(spec, budget=min(spec.budget, remaining + steps_used))
        return run_turn(spec, ctx, self.llm, seq, notes, steps_used, lambda: ctx.iid in self._cancelled, self.emit,
                        announce)

    def _triage(self, ctx: ToolContext, seq: Seq) -> TurnResult:
        if not ep.acquire_lock(self.g, ctx.iid, "triage", wall_ms(), LOCK_TTL_MS):  # before any write: one writer only
            return TurnResult("another writer holds this incident", "lock_lost", 0, 0)
        ep.set_status(self.g, ctx.iid, "investigating", ctx.now)
        self.emit("agent_started", {"agent": "triage", "incident": ctx.iid})
        # Memory lookup is Triage's first tool call, run up front so every run (and every memory mode) does the same
        # thing and the Triage model does not spend a round trip on it. It is still a recorded, counted step.
        self.emit("tool_called", {"agent": "triage", "tool": "recall_similar", "args": {}})
        out = execute(ctx, "triage", "recall_similar", {}, seq.action(), "memory lookup at incident start")
        self.emit("tool_result", {"agent": "triage", "tool": "recall_similar", "outcome": out.outcome,
                                  "result": out.result, "action_id": out.action_id})
        return self._turn(ctx, seq, "triage", steps_used=1, announce=False)

    def _pick_fast_path(self, ctx: ToolContext) -> dict | None:
        if ctx.mode != "graph":
            return None
        rows = self.g.rows(
            "MATCH (:Incident {id: $iid})-[s:SIMILAR_TO]->(p:Incident)-[:RESOLVED_BY]->(pb:Playbook {status: 'active'}) "
            "WHERE s.score >= $min AND pb.weight >= $w "
            "RETURN pb.id AS playbook, pb.name AS name, pb.weight AS weight, max(s.score) AS similarity, "
            "pb.last_used_at AS last_used ORDER BY weight DESC, similarity DESC, last_used DESC LIMIT 1",
            {"iid": ctx.iid, "min": config.FAST_PATH_MIN_SIMILARITY, "w": config.FAST_PATH_WEIGHT})
        return rows[0] if rows else None

    def _fast_path_of(self, iid: str) -> dict | None:
        rows = self.g.rows("MATCH (i:Incident {id: $iid}) WHERE i.fast_path_playbook IS NOT NULL "
                           "MATCH (pb:Playbook {id: i.fast_path_playbook}) "
                           "RETURN pb.id AS playbook, pb.name AS name, pb.weight AS weight", {"iid": iid})
        return rows[0] if rows else None

    def _fast_path_note(self, ctx: ToolContext, fp: dict) -> str:
        steps = pr.steps_of(self.g, fp["playbook"])
        text = "; ".join(f"{s['step']}. {s['tool']}({', '.join(f'{k}={v}' for k, v in s['args'].items())})"
                         for s in steps)
        return (f"FAST PATH: memory found playbook {fp['playbook']} \"{fp['name']}\" (weight {round(fp['weight'], 2)}) "
                f"that resolved a similar past incident. Steps: {text}. Bindings: $origin = {ctx.origin()}. Bind any "
                f"other placeholder from the symptoms (for example the saturated datastore) or from recent flag "
                f"changes. This is a suggestion from memory, not proof: verify its precondition with one evidence call "
                f"before the first state-changing step, and take no action if the evidence does not support it. "
                f"Then follow the steps and check_health.")

    def _bounce_note(self, bounce: int) -> str:
        return ("A remediation attempt failed. Hypotheses behind the failed actions are marked rejected: look for a "
                "different cause.") if bounce else ""

    def _reject_failed_hypotheses(self, ctx: ToolContext) -> None:
        """Hypotheses that point at a node the Remediator acted on without effect are no longer credible."""
        rows = self.g.read("MATCH (:Incident {id: $i})-[:TOOK]->(a:Action) WHERE a.agent = 'remediator' "
                           "AND a.outcome IN ['no_effect', 'harmful'] RETURN a.args_json", {"i": ctx.iid})
        targets = set()
        for (blob,) in rows:
            targets.update(str(v) for v in json.loads(blob).values())
        if targets:
            self.g.write("MATCH (h:Hypothesis {incident_id: $i})-[:POINTS_TO]->(t) "
                         "WHERE h.status = 'open' AND t.name IN $targets SET h.status = 'rejected'",
                         {"i": ctx.iid, "targets": sorted(targets)})

    # ---- endings ---------------------------------------------------------------------------------------------------
    def _resolved_reason(self, iid: str) -> str:
        """self_healed when the system recovered without any remediation having worked."""
        n = self.g.read("MATCH (:Incident {id: $i})-[:TOOK]->(a:Action) WHERE a.outcome = 'success' AND a.tool IN "
                        "['rollback','restart','scale','set_flag','failover','renew_cert'] RETURN count(a)",
                        {"i": iid})[0][0]
        return "fixed" if n else "self_healed"

    def _finish(self, ctx: ToolContext, seq: Seq, status: str, reason: str, fast: bool) -> IncidentResult:
        ep.set_status(self.g, ctx.iid, status, ctx.now, reason)
        ep.mark_resolution_cost(self.g, ctx.iid)
        if status == "failed":
            self._escalate(ctx)
            self.emit("failed", {"incident": ctx.iid, "reason": reason})
        else:
            self.emit("resolved", {"incident": ctx.iid, "reason": reason})
        self.emit("state_changed", {"incident": ctx.iid, "state": "CONSOLIDATE"})
        report = self._scribe(ctx, seq, status, reason)
        return self._result(ctx, status, reason, report, fast)

    def _escalate(self, ctx: ToolContext) -> None:
        owners = self.g.rows(q.Q8_OWNERS, {"svc": ctx.origin()})
        if owners:
            self.g.write("MATCH (i:Incident {id: $i}) SET i.escalation_team = $t, i.escalation_channel = $c",
                         {"i": ctx.iid, "t": owners[0]["team"], "c": owners[0]["channel"]})

    def _scribe(self, ctx: ToolContext, seq: Seq, status: str, reason: str = "") -> dict:
        """Always runs, even on failure. Consolidation and people are deterministic; the postmortem text uses the fast
        model and falls back to a stub if the LLM is unavailable."""
        self.emit("agent_started", {"agent": "scribe", "incident": ctx.iid})
        report: dict = {}
        for tool in SCRIBE_ACTIONS:
            self.emit("tool_called", {"agent": "scribe", "tool": tool, "args": {}})
            out = execute(ctx, "scribe", tool, {}, seq.action())
            self.emit("tool_result", {"agent": "scribe", "tool": tool, "outcome": out.outcome, "result": out.result,
                                      "action_id": out.action_id})
            if tool == "consolidate":
                report = out.result
        view = ep.incident_view(self.g, ctx.iid)
        text, pm_msg = None, None
        try:
            record = json.dumps({"title": view["title"], "status": status, "reason": reason, "steps": view["steps"],
                                 "actions": [f"{a['agent']} {a['tool']}({a['args']}) -> {a['outcome']}"
                                             for a in view["actions"] if a["agent"] != "scribe"],
                                 "root_causes": view["root_causes"], "learned": report})[:2500]
            res = self.llm.chat("fast", [{"role": "system", "content": prompts.SCRIBE_POSTMORTEM},
                                         {"role": "user", "content": record}], [])
            record_llm_call(ctx, res)
            text = res.content
            pm_msg = new_id()
            ep.add_message(self.g, ctx.iid, pm_msg, "scribe", "assistant", f"[postmortem] {text}", seq.message(),
                           ctx.now, res.usage)
        except LLMUnavailable:
            log.warning("postmortem for %s written as a stub: LLM unavailable", ctx.iid)
        pm_args = {"text": text or stub_postmortem(view, report)}
        self.emit("tool_called", {"agent": "scribe", "tool": "write_postmortem", "args": pm_args})
        out = execute(ctx, "scribe", "write_postmortem", pm_args, seq.action())
        if pm_msg:
            ep.link_message_action(self.g, pm_msg, out.action_id)
        self.emit("tool_result", {"agent": "scribe", "tool": "write_postmortem", "outcome": out.outcome,
                                  "result": out.result, "action_id": out.action_id})
        return report

    def _result(self, ctx: ToolContext, status: str, reason: str, report: dict, fast: bool | None = None
                ) -> IncidentResult:
        row = self.g.rows("MATCH (i:Incident {id: $i}) RETURN i.steps_to_resolve AS s, i.tokens_to_resolve AS t, "
                          "i.steps AS steps, i.tokens AS tokens, i.fast_path_playbook AS fp, "
                          "i.cost_to_resolve AS cost, i.cost_usd AS cost_all", {"i": ctx.iid})[0]
        harm = self.world.truth(ctx.iid)["harm"] if ctx.iid in self.world.faults else 0
        return IncidentResult(ctx.iid, status, reason, row["s"] if row["s"] is not None else row["steps"],
                              row["t"] if row["t"] is not None else row["tokens"],
                              fast_path=bool(row["fp"]) if fast is None else fast,
                              handoffs=len(ep.handoffs(self.g, ctx.iid)), harmful_actions=harm, report=report,
                              cost_usd=round(row["cost"] if row["cost"] is not None else (row["cost_all"] or 0.0), 6))

    # ---- helpers ---------------------------------------------------------------------------------------------------
    def _handoff(self, ctx: ToolContext, frm: str, to: str, hseq: int, summary: str) -> None:
        ep.write_handoff(self.g, ctx.iid, frm, to, hseq, summary, ctx.now)
        self.emit("handoff", {"incident": ctx.iid, "from": frm, "to": to, "summary": summary})

    def _steps(self, ctx: ToolContext) -> int:
        return self.g.read("MATCH (i:Incident {id: $i}) RETURN i.steps", {"i": ctx.iid})[0][0]

    def _stop_reason(self, ctx: ToolContext, deadline: float) -> str | None:
        if ctx.iid in self._cancelled:
            return "cancelled"
        if self._steps(ctx) >= self.max_steps:
            return "budget"
        if time.monotonic() > deadline:
            return "timeout"
        return None

    def _has_postmortem(self, iid: str) -> bool:
        return bool(self.g.read("MATCH (i:Incident {id: $i}) RETURN i.postmortem", {"i": iid})[0][0])

    def _resume_agent(self, iid: str) -> str:
        """After a crash: continue with whoever was handed the incident last (Triage if nobody was)."""
        chain = ep.handoffs(self.g, iid)
        return chain[-1]["to"] if chain else "triage"


def resume_all(coordinator: Coordinator) -> list[IncidentResult]:
    """On boot: continue every active incident whose lock is free or expired (PRD §13)."""
    return [coordinator.run(iid) for iid in ep.resumable_incidents(coordinator.g, wall_ms())]
