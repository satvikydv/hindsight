"""A deterministic stand-in for the LLM crew: an expert system that reads tool results and context like an SRE would.

Why it exists: (1) it lets the whole benchmark pipeline run offline, for free, over every fault class; (2) it is the
"scripted policy" the PRD wants for the concurrency benchmark, so that measures the graph and not the model.

What it is NOT: evidence about memory. It never reads simulator ground truth, but it is hand-written, it ignores the
vector-mode postmortem text, and it follows a playbook by a fixed rule. Whether graph memory beats vector memory is
measured with the real model, not with this.
"""

import json
import re

from api.llm import LLMResult, ScriptedLLM, ToolCall
from api.sim.generator import DATASTORES

DATASTORE_NAMES = {n for n, _ in DATASTORES}
RESULT_RE = re.compile(r"^<tool_result[^>]*>(.*)</tool_result>$", re.S)
HYP_RE = re.compile(r"^\s+\[([\d.]+)\] (.*) -> (\w+) (\S+) \((\w+)\) id=(\S+)$", re.M)
ACTION_RE = re.compile(r"^\s+#\d+ (\w+) (\w+)\((.*?)\) -> (\w+):", re.M)       # agent, tool, args, outcome
STEP_RE = re.compile(r"\d+\. (\w+)\((.*?)\)(?=;|\.|$)")
ACT_TOOLS = ("rollback", "restart", "scale", "set_flag", "failover", "renew_cert")
SCORE = 0.85


def history(messages: list[dict]) -> list[tuple[str, dict, dict]]:
    """(tool, args, result) for every tool call already made in this conversation."""
    calls, out = {}, []
    for m in messages:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                calls[tc["id"]] = (tc["function"]["name"], json.loads(tc["function"]["arguments"] or "{}"))
        elif m["role"] == "tool" and m.get("tool_call_id") in calls:
            name, args = calls[m["tool_call_id"]]
            body = RESULT_RE.match(m["content"])
            try:
                result = json.loads(body.group(1)) if body else {}
            except json.JSONDecodeError:
                result = {}
            out.append((name, args, result))
    return out


class Ctx:
    """The facts an agent can read from the context message."""

    def __init__(self, text: str):
        self.text = text
        self.origin = re.search(r"origin service: (\S+)", text).group(1)
        self.symptoms: dict[str, dict[str, float]] = {}
        m = re.search(r"^symptoms: (.*)$", text, re.M)
        for part in (m.group(1).split("; ") if m and m.group(1) else []):
            node, _, kinds = part.partition(": ")
            self.symptoms[node] = {k.split("=")[0]: float(k.split("=")[1]) for k in kinds.split(", ") if "=" in k}
        self.fast = "FAST PATH:" in text
        steps = re.search(r"Steps: (.*?)\. Bindings", text, re.S)
        self.playbook = [(t, dict(a.split("=", 1) for a in args.split(", ") if "=" in a))
                         for t, args in STEP_RE.findall(steps.group(1) + ".")] if steps else []
        self.hypotheses = [{"score": float(s), "text": t, "status": st} for s, t, _, _, st, _ in HYP_RE.findall(text)]
        # actions the crew already took (the "actions so far" block): a new turn is a new conversation, so what was
        # already applied must come from here, exactly as a real model would read it
        self.applied = [(tool, dict(a.split("=", 1) for a in args.split(", ") if "=" in a))
                        for agent, tool, args, outcome in ACTION_RE.findall(text)
                        if agent == "remediator" and tool in ACT_TOOLS and outcome == "success"]

    def kinds_at(self, node: str) -> dict:
        return self.symptoms.get(node, {})

    @property
    def saturated_datastore(self) -> str | None:
        return next((n for n, k in self.symptoms.items() if n in DATASTORE_NAMES and "saturation" in k), None)

    @property
    def top_caller(self) -> str:
        """The service with the worst latency: where a connection-pool problem hurts most."""
        lat = {n: k["latency"] for n, k in self.symptoms.items() if "latency" in k and n not in DATASTORE_NAMES}
        return max(lat, key=lat.get) if lat else self.origin


def _call(messages: list[dict], name: str, **args) -> LLMResult:
    return LLMResult(None, [ToolCall(f"call_{len(messages)}", name, args)])


def _done(name_args: tuple[str, dict], hist: list) -> bool:
    name, args = name_args
    return any(h[0] == name and h[1] == args for h in hist)


class ExpertPolicy:
    def __call__(self, tier: str, messages: list[dict], tools: list[dict]) -> LLMResult:
        system = messages[0]["content"]
        offered = {t["function"]["name"] for t in tools}
        if "You are the Scribe" in system:
            r = LLMResult("The incident was handled by the crew; the fix and its cause are recorded in the graph.")
        elif offered == {"finish"}:                                  # wrap-up call at the end of a budget
            r = LLMResult(None, [ToolCall("wrap", "finish", {"summary": "ran out of budget; see the actions taken"})])
        else:
            ctx, hist = Ctx(messages[1]["content"]), history(messages)
            agent = ("triage" if "You are Triage" in system else
                     "investigator" if "You are the Investigator" in system else "remediator")
            r = getattr(self, agent)(messages, ctx, hist)
        # report plausible usage: input grows with the conversation, output is a short tool call
        r.prompt_tokens = max(50, len(json.dumps(messages)) // 4)
        r.completion_tokens = 30
        r.tokens = r.prompt_tokens + r.completion_tokens
        return r

    # ---- Triage ----------------------------------------------------------------------------------------------------
    def triage(self, messages, ctx, hist) -> LLMResult:
        if not any(h[0] == "blast_radius" for h in hist):
            return _call(messages, "blast_radius", service=ctx.origin)
        blast = next(h[2] for h in hist if h[0] == "blast_radius")
        crit = sum(1 for a in blast.get("affected", []) if a.get("tier") == "critical")
        if not any(h[0] == "set_severity" for h in hist):
            sev = "sev1" if crit >= 2 else "sev2" if crit == 1 else "sev3"
            return _call(messages, "set_severity", severity=sev, reason=f"{crit} critical services in the blast radius")
        memory = " Memory has a playbook for this." if "resolved by playbook" in ctx.text else ""
        return LLMResult(None, [ToolCall("fin", "finish", {"summary": f"Origin {ctx.origin}; {len(blast['affected'])} "
                                                                       f"services affected.{memory}"})])

    # ---- Investigator ----------------------------------------------------------------------------------------------
    def investigator(self, messages, ctx, hist) -> LLMResult:
        if not any(h[0] == "rank_suspects" for h in hist):
            return _call(messages, "rank_suspects")
        causes = self._causes(ctx, hist)
        if causes is None:                                           # nothing in the changes: read the logs
            return _call(messages, "get_logs", service=ctx.origin)
        for c in causes:
            propose = ("propose_hypothesis", {"text": c["text"], "score": SCORE, "target_type": c["type"],
                                              "target": c["ref"]})
            confirm = ("confirm_root_cause", {"target_type": c["type"], "target": c["ref"]})
            for step in (propose, confirm):
                if not _done(step, hist):
                    return _call(messages, step[0], **step[1])
        return LLMResult(None, [ToolCall("fin", "finish", {"summary": "Root cause: " + "; ".join(
            c["text"] for c in causes)})])

    def _causes(self, ctx: Ctx, hist) -> list[dict] | None:
        rank = next(h[2] for h in hist if h[0] == "rank_suspects")
        causes = []
        for s in rank.get("suspects", []):
            for ch in s["recent_changes"]:
                if ch["type"] == "deploy":
                    causes.append({"type": "Deploy", "ref": ch["id"], "text": f"bad deploy {ch['version']} on {s['node']}"})
                else:
                    causes.append({"type": "ConfigFlag", "ref": ch["key"], "text": (
                        f"config flag {ch['key']} changed from {ch.get('previous')} to {ch['value']} on {s['node']}")})
        ds = ctx.saturated_datastore
        if ds:
            causes.append({"type": "Datastore", "ref": ds, "text": f"connection pool exhausted on {ds}"})
        if causes:
            return causes
        kinds = ctx.kinds_at(ctx.origin)
        if "crashloop" in kinds:
            return [{"type": "Service", "ref": ctx.origin, "text": f"OOMKilled memory leak on {ctx.origin}"}]
        if "cert" in kinds:
            return [{"type": "Service", "ref": ctx.origin, "text": f"tls certificate expired on {ctx.origin}"}]
        logs = next((h[2] for h in hist if h[0] == "get_logs"), None)
        if logs is None:
            return None
        text = " ".join(logs.get("lines", []))
        if "no healthy replicas" in text:
            return [{"type": "Service", "ref": ctx.origin, "text": f"no healthy replicas on {ctx.origin}"}]
        if "queue depth" in text:
            return [{"type": "Service", "ref": ctx.origin, "text": f"queue depth overload on {ctx.origin}"}]
        return [{"type": "Service", "ref": ctx.origin, "text": f"unknown fault on {ctx.origin}"}]

    # ---- Remediator ------------------------------------------------------------------------------------------------
    def remediator(self, messages, ctx, hist) -> LLMResult:
        rc = next((h[2] for h in hist if h[0] == "recent_changes"), None)
        if ctx.fast:
            if rc is None:                                           # verify the playbook's precondition first
                return _call(messages, "recent_changes", service=ctx.origin, since_minutes=360)
            why = self._playbook_blocked(ctx, rc)
            if why:
                return LLMResult(None, [ToolCall("fin", "finish", {"summary": f"playbook does not apply: {why}"})])
            plan = self._bind(ctx, rc)
        else:
            plan = self._plan_from_hypotheses(ctx)
        if not plan:
            return LLMResult(None, [ToolCall("fin", "finish", {"summary": "no actionable cause"})])

        acts = [(h[0], h[1], h[2]) for h in hist if h[0] in ACT_TOOLS]
        if acts and not self._applied(acts[-1][2]):
            return LLMResult(None, [ToolCall("fin", "finish", {"summary": "last action did not help: " + str(acts[-1][2])})])
        applied = [(a[0], a[1]) for a in acts if self._applied(a[2])] + ctx.applied
        for i, step in enumerate(plan):
            if _done(step, applied):
                continue
            # a rollback after a restart is the "if it recurs" step of a memory leak: only if still unhealthy
            if step[0] == "rollback" and i > 0 and plan[i - 1][0] == "restart":
                checks = [h for h in hist if h[0] == "check_health"]
                if not checks:
                    return _call(messages, "check_health", service=ctx.origin)
                if checks[-1][2].get("status") == "green":
                    return LLMResult(None, [ToolCall("fin", "finish", {"summary": "healthy after the restart"})])
            if step[0] == "failover":    # guarded: needs a hypothesis >= 0.7 on the target; the check above is the evidence
                svc = step[1]["service"]
                hyp = ("propose_hypothesis", {"text": f"no healthy replicas on {svc}", "score": SCORE,
                                              "target_type": "Service", "target": svc})
                if not _done(hyp, hist) and not any(h["score"] >= 0.7 and svc in h["text"] and h["status"] != "rejected"
                                                    for h in ctx.hypotheses):
                    return _call(messages, hyp[0], **hyp[1])
            return _call(messages, step[0], **step[1])
        last = [h for h in hist if h[0] == "check_health"]
        if not last or hist.index(last[-1]) < max(hist.index(h) for h in hist if h[0] in ACT_TOOLS):
            return _call(messages, "check_health", service=ctx.origin)
        return LLMResult(None, [ToolCall("fin", "finish", {"summary": "still red after the full plan"})])

    @staticmethod
    def _applied(result: dict) -> bool:
        return "applied" in str(result.get("detail", ""))

    def _bind(self, ctx: Ctx, rc: dict) -> list[tuple[str, dict]]:
        flag = (rc.get("flag_changes") or [{}])[0]
        values = {"$origin": ctx.origin, "$datastore": ctx.saturated_datastore or "?", "$top_caller": ctx.top_caller,
                  "$changed_flag": flag.get("key", "?"), "$previous_value": flag.get("previous") or "?"}
        return [(tool, {k: values.get(v, v) for k, v in args.items()}) for tool, args in ctx.playbook]

    def _playbook_blocked(self, ctx: Ctx, rc: dict) -> str | None:
        """Why the playbook does not apply here, or None. One cheap evidence check per kind of step."""
        tools = {t for t, _ in ctx.playbook}
        deploys, flags = bool(rc.get("deploys")), bool(rc.get("flag_changes"))
        kinds = ctx.kinds_at(ctx.origin)
        ds = ctx.saturated_datastore
        if "rollback" in tools and not deploys:
            return "no recent deploy to roll back"
        if "set_flag" in tools and not flags:
            return "no recent flag change"
        if any(a.get("target") == "$datastore" for t, a in ctx.playbook if t == "scale") and not ds:
            return "no saturated datastore"
        if tools == {"restart"} or tools == {"restart", "rollback"}:
            if "crashloop" not in kinds and not ds:
                return "no crashloop"
        if "renew_cert" in tools and "cert" not in kinds:
            return "no expired certificate"
        quiet = not deploys and not flags and not ds and not {"crashloop", "cert"} & set(kinds)
        if "failover" in tools and not (quiet and "error_rate" in kinds):
            return "origin is not hard-down"
        if tools == {"scale"} and not (quiet and "latency" in kinds):
            return "no overload"
        return None

    def _plan_from_hypotheses(self, ctx: Ctx) -> list[tuple[str, dict]]:
        plan_ds, plan_other = [], []
        for h in ctx.hypotheses:
            if h["status"] == "rejected" or h["score"] < 0.7:
                continue
            t = h["text"]
            if m := re.match(r"bad deploy \S+ on (\S+)", t):
                plan_other.append(("rollback", {"service": m.group(1)}))
            elif m := re.match(r"config flag (\S+) changed from (\S+) to \S+ on", t):
                plan_other.append(("set_flag", {"key": m.group(1), "value": m.group(2)}))
            elif m := re.match(r"connection pool exhausted on (\S+)", t):
                plan_ds += [("scale", {"target": m.group(1)}), ("restart", {"service": ctx.top_caller})]
            elif m := re.match(r"OOMKilled memory leak on (\S+)", t):
                plan_other += [("restart", {"service": m.group(1)}), ("rollback", {"service": m.group(1)})]
            elif m := re.match(r"tls certificate expired on (\S+)", t):
                plan_other.append(("renew_cert", {"service": m.group(1)}))
            elif m := re.match(r"no healthy replicas on (\S+)", t):
                plan_other.append(("failover", {"service": m.group(1)}))
            elif m := re.match(r"queue depth overload on (\S+)", t):
                plan_other.append(("scale", {"target": m.group(1)}))
        return plan_ds + plan_other                   # deepest first: the datastore fix, then the service fixes


def expert_llm() -> ScriptedLLM:
    return ScriptedLLM(ExpertPolicy())
