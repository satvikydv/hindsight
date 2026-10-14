"""Remediation tools: the only tools that change the (simulated) world. Guardrails run before every one of these.
Registered on import.
"""

from api.memory import procedural as pr
from api.tools.base import Tool, ToolContext, register
from api.tools.graph_tools import REMEDIATOR, STR


def _act(tool: str, target_arg: str):
    def run(ctx: ToolContext, **args) -> dict:
        res = ctx.world.act(ctx.iid, tool, args[target_arg], args.get("value"))
        return {"_outcome": res["outcome"], "detail": res["detail"], "target": args[target_arg]}

    return run


def _cite_owner_service(ctx, args, result):
    if "key" in args:  # set_flag: cite the service that owns the flag
        rows = ctx.g.read("MATCH (s:Service)-[h:HAS_FLAG]->(:ConfigFlag {key: $k}) WHERE h.valid_to IS NULL "
                          "RETURN s.name", {"k": args["key"]})
        return ctx.refs([r[0] for r in rows], "remediation target")
    return ctx.refs([args.get("service") or args.get("target")], "remediation target")


def _playbook_steps(ctx: ToolContext, playbook_id: str) -> dict:
    ctx.world.touch()
    steps = pr.steps_of(ctx.g, playbook_id)
    if not steps:
        raise LookupError(f"no playbook {playbook_id!r}")
    return {"playbook_id": playbook_id, "steps": steps}


def _check_health(ctx: ToolContext, service: str) -> dict:
    ctx.world.touch()
    return ctx.world.check_health(ctx.iid, service)


for _t in [
    Tool("playbook_steps", "The ordered steps of a playbook (tool + argument template). Placeholders like $origin "
         "are filled in from this incident.", {"playbook_id": STR}, ["playbook_id"], {REMEDIATOR}, _playbook_steps,
         lambda ctx, a, r: [("Playbook", a["playbook_id"], "playbook being followed")]),
    Tool("rollback", "Roll a service back to its previous deploy.", {"service": STR}, ["service"], {REMEDIATOR},
         _act("rollback", "service"), _cite_owner_service, acts=True),
    Tool("restart", "Restart a service. Tier-critical services allow one restart per incident.",
         {"service": STR}, ["service"], {REMEDIATOR}, _act("restart", "service"), _cite_owner_service, acts=True),
    Tool("scale", "Add capacity to a service or datastore.", {"target": STR}, ["target"], {REMEDIATOR},
         _act("scale", "target"), _cite_owner_service, acts=True),
    Tool("set_flag", "Set a config flag to a value (use the previous value to undo a bad change).",
         {"key": STR, "value": STR}, ["key", "value"], {REMEDIATOR}, _act("set_flag", "key"), _cite_owner_service,
         acts=True),
    Tool("failover", "Fail a service over to its standby. Needs a hypothesis with score >= 0.7 pointing at it.",
         {"service": STR}, ["service"], {REMEDIATOR}, _act("failover", "service"), _cite_owner_service, acts=True),
    Tool("renew_cert", "Renew a service's TLS certificate.", {"service": STR}, ["service"], {REMEDIATOR},
         _act("renew_cert", "service"), _cite_owner_service, acts=True),
    Tool("check_health", "Whether a service is green or red right now. The incident is resolved only when the "
         "affected services are green.", {"service": STR}, ["service"], {REMEDIATOR}, _check_health,
         lambda ctx, a, r: ctx.refs([a["service"]], "health check")),
]:
    register(_t)
