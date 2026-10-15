"""A test LLM that replays per-agent plans, so coordinator behaviour is tested deterministically with no tokens.

plans = {"triage": [turn_plan, ...], "investigator": [...], "remediator": [...]} where a turn_plan is a list of
(tool, args) and args may be a dict or a callable(env) -> dict. Each new agent turn (a conversation with just the system
and context messages) moves on to that agent's next turn_plan; the last one repeats. When a plan runs out the agent
calls finish. ("__crash__", {}) raises, to simulate the server dying mid-turn.
"""

import re
from types import SimpleNamespace

from api.llm import LLMResult, LLMUnavailable, ScriptedLLM, ToolCall

AGENTS = {"You are Triage": "triage", "You are the Investigator": "investigator",
          "You are the Remediator": "remediator", "You are the Scribe": "scribe"}


class Crash(RuntimeError):
    pass


class Crew:
    def __init__(self, plans: dict, env=None, postmortem: str = "Blameless postmortem text.", down: bool = False):
        self.plans, self.env, self.postmortem, self.down = plans, env or (lambda: SimpleNamespace()), postmortem, down
        self.turns: dict[str, int] = {}

    def __call__(self, tier, messages, tools) -> LLMResult:
        if self.down:
            raise LLMUnavailable("simulated outage")
        system = messages[0]["content"]
        agent = next(name for marker, name in AGENTS.items() if marker in system)
        if agent == "scribe":
            return LLMResult(self.postmortem)
        if hasattr(self.env, "bind"):      # the API creates incident ids server-side: learn it from the context
            m = re.search(r"INCIDENT (\w{26}):", messages[1]["content"])
            if m:
                self.env.bind(m.group(1))
        if len(messages) == 2:
            self.turns[agent] = self.turns.get(agent, 0) + 1
        turns = self.plans[agent]
        plan = turns[min(self.turns[agent], len(turns)) - 1]
        done = sum(1 for m in messages if m.get("role") == "tool")
        if done >= len(plan):
            return LLMResult(None, [ToolCall("fin", "finish", {"summary": f"{agent} finished"})])
        tool, args = plan[done]
        if tool == "__crash__":
            raise Crash("server died")
        return LLMResult(None, [ToolCall(f"c{done}", tool, args(self.env()) if callable(args) else args)])


def scripted(plans: dict, env=None, **kw) -> ScriptedLLM:
    return ScriptedLLM(Crew(plans, env, **kw), tokens_per_call=100)
