"""The agent loop (PRD §7). One turn:

  load context from the graph -> call the LLM with only this agent's tools -> run each tool call through the memory API
  (write-ahead Action, citations, outcome) -> stop on finish / budget / failure -> hand back to the coordinator.

Stop reasons: finished, auto_finished, budget, loop, repeated_errors, failed_actions, no_action, token_cap,
cancelled, lock_lost. An LLM outage raises LLMUnavailable to the coordinator.
"""

import json
from dataclasses import dataclass, field
from typing import Callable

from api import config
from api.agents.context import build_context
from api.agents.prompts import PROMPTS
from api.clock import wall_ms
from api.ids import new_id
from api.llm import LLM, LLMUnavailable
from api.memory import episodic as ep
from api.tools import ToolContext, ToolOutcome, execute, schemas_for

LOOP_REPEATS = 3          # same tool + args + result this many times means the agent is stuck
MAX_CONSECUTIVE_ERRORS = 2
MAX_FAILED_ACTIONS = 3
LOCK_TTL_MS = 30_000

FINISH_SCHEMA = {"type": "function", "function": {
    "name": "finish",
    "description": "End your turn and hand off. summary: one or two sentences for the next agent.",
    "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
                   "additionalProperties": False}}}


@dataclass(frozen=True)
class AgentSpec:
    name: str
    tier: str          # strong | fast model
    budget: int        # tool calls per turn (PRD §7)


SPECS = {
    "triage": AgentSpec("triage", "fast", 4),
    "investigator": AgentSpec("investigator", "strong", 8),
    "remediator": AgentSpec("remediator", "strong", 6),
}


class Seq:
    """Per-incident counters, seeded from the graph so a resumed incident keeps numbering."""

    def __init__(self, ctx: ToolContext):
        a = ctx.g.read("MATCH (:Incident {id: $i})-[t:TOOK]->() RETURN max(t.seq)", {"i": ctx.iid})[0][0]
        m = ctx.g.read("MATCH (:Message)-[l:IN_LOG]->(:Incident {id: $i}) RETURN max(l.seq)", {"i": ctx.iid})[0][0]
        self._action, self._message = a or 0, m or 0

    def action(self) -> int:
        self._action += 1
        return self._action

    def message(self) -> int:
        self._message += 1
        return self._message


@dataclass
class TurnResult:
    summary: str
    reason: str
    steps: int                 # tool calls this agent made in the turn, including any carried in via steps_used
    tokens: int
    outcomes: list[ToolOutcome] = field(default_factory=list)


Emit = Callable[[str, dict], None]


def _persist(ctx: ToolContext, seq: Seq, agent: str, role: str, content: str, usage: dict | None = None) -> str:
    """Store one conversation message; returns its id so actions can be linked back to the turn that caused them."""
    mid = new_id()
    ep.add_message(ctx.g, ctx.iid, mid, agent, role, content, seq.message(), ctx.now, usage)
    return mid


def record_llm_call(ctx: ToolContext, res) -> None:
    """Add one call's tokens, dollars and latency to the incident's running totals."""
    ep.add_usage(ctx.g, ctx.iid, tokens=res.tokens, cost=res.cost_usd or 0.0, llm_ms=res.latency_ms)


def _wrap_up(spec: AgentSpec, ctx: ToolContext, llm: LLM, messages: list[dict], seq: Seq) -> tuple[str, int]:
    """Out of tool budget: one last call with only `finish` available, so the handoff still says what was learned.
    Costs one LLM call but no step. Falls back to a plain note if the model does not comply or is unreachable."""
    fallback = "step budget used up before finishing"
    try:
        res = llm.chat(spec.tier, messages + [{"role": "user", "content":
                       "Your tool budget is used up. Call finish(summary) now with what you found or did."}],
                       [FINISH_SCHEMA])
    except LLMUnavailable:
        return fallback, 0
    record_llm_call(ctx, res)
    finish = next((c for c in res.tool_calls if c.name == "finish"), None)
    summary = (finish.args.get("summary") if finish else None) or res.content or fallback
    _persist(ctx, seq, spec.name, "assistant", f"[wrap-up] {summary}", res.usage)
    return summary, res.tokens


def _auto_finish(agent: str, out: ToolOutcome) -> str | None:
    """The Remediator is done the moment a health check comes back green."""
    if agent == "remediator" and out.name == "check_health" and out.result.get("status") == "green":
        return f"{out.result.get('service')} is green after remediation"
    return None


def run_turn(spec: AgentSpec, ctx: ToolContext, llm: LLM, seq: Seq, notes: str = "", steps_used: int = 0,
             should_stop: Callable[[], bool] | None = None, emit: Emit | None = None,
             announce: bool = True) -> TurnResult:
    emit = emit or (lambda event, data: None)
    tools = schemas_for(spec.name) + [FINISH_SCHEMA]
    context = build_context(ctx, spec.name, notes)
    messages = [{"role": "system", "content": PROMPTS[spec.name]}, {"role": "user", "content": context}]
    _persist(ctx, seq, spec.name, "user", context)
    if announce:
        emit("agent_started", {"agent": spec.name, "incident": ctx.iid})

    steps, tokens = steps_used, 0
    outcomes: list[ToolOutcome] = []
    consecutive_errors = failed_actions = 0
    seen: dict[tuple, int] = {}
    nudged = False

    def done(summary: str, reason: str) -> TurnResult:
        ep.release_lock(ctx.g, ctx.iid, spec.name)
        return TurnResult(summary, reason, steps, tokens, outcomes)

    while True:
        if should_stop and should_stop():
            return done("", "cancelled")
        if steps >= spec.budget:
            summary, spent = _wrap_up(spec, ctx, llm, messages, seq)
            tokens += spent
            return done(summary, "budget")
        used = ctx.g.read("MATCH (i:Incident {id: $i}) RETURN i.tokens", {"i": ctx.iid})[0][0]
        if used >= config.MAX_TOKENS_PER_INCIDENT:
            return done("token cap reached", "token_cap")
        if not ep.acquire_lock(ctx.g, ctx.iid, spec.name, wall_ms(), LOCK_TTL_MS):
            return done("another writer holds this incident", "lock_lost")

        res = llm.chat(spec.tier, messages, tools)       # LLMUnavailable propagates to the coordinator
        tokens += res.tokens
        record_llm_call(ctx, res)
        messages.append(res.assistant_message)
        calls_text = "; ".join(f"{c.name}({json.dumps(c.args)})" for c in res.tool_calls)
        msg_id = _persist(ctx, seq, spec.name, "assistant", " | ".join(x for x in (res.content, calls_text) if x),
                          res.usage)

        if not res.tool_calls:
            if nudged:
                return done(res.content or "", "no_action")
            nudged = True
            messages.append({"role": "user", "content": "Call one of your tools, or call finish(summary)."})
            continue

        for call in res.tool_calls:
            if call.name == "finish":
                return done(call.args.get("summary") or res.content or "", "finished")
            if steps >= spec.budget:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": "step budget exhausted"})
                continue
            emit("tool_called", {"agent": spec.name, "tool": call.name, "args": call.args})
            out = execute(ctx, spec.name, call.name, call.args, seq.action(), res.content or "", call.args_error)
            steps += 1
            outcomes.append(out)
            ep.link_message_action(ctx.g, msg_id, out.action_id)       # why: the reasoning that led to this action
            messages.append({"role": "tool", "tool_call_id": call.id, "content": out.text})
            _persist(ctx, seq, spec.name, "tool", out.text)
            emit("tool_result", {"agent": spec.name, "tool": call.name, "outcome": out.outcome,
                                 "result": out.result, "action_id": out.action_id})

            consecutive_errors = consecutive_errors + 1 if out.outcome == "error" else 0
            failed_actions += out.outcome in ("no_effect", "harmful")
            key = (call.name, json.dumps(call.args, sort_keys=True), hash(out.text))
            seen[key] = seen.get(key, 0) + 1

            if (summary := _auto_finish(spec.name, out)):
                return done(summary, "auto_finished")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                return done(f"stopped after repeated errors: {out.result.get('error', '')}", "repeated_errors")
            if seen[key] >= LOOP_REPEATS:
                return done(f"stuck repeating {call.name}", "loop")
            if failed_actions >= MAX_FAILED_ACTIONS:
                return done("three actions failed to help", "failed_actions")
