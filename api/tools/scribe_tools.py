"""Scribe tools. The Scribe is mostly deterministic, but its work is recorded like any other agent's: each step is a
write-ahead Action, so the timeline and the path view show what was learned. Registered on import.
"""

from pathlib import Path

from api.memory import consolidate as cons
from api.memory import episodic as ep
from api.tools.base import Tool, ToolContext, register

SCRIBE = "scribe"
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "postmortems"
SUMMARY_CHARS = 600  # large text stays out of the graph: a short summary on the node, the full text in a blob


def _consolidate(ctx: ToolContext) -> dict:
    return cons.consolidate(ctx.g, ctx.embedder, ctx.iid, ctx.now)


def _link_people(ctx: ToolContext) -> dict:
    return cons.link_people(ctx.g, ctx.iid, ctx.now)


def _write_postmortem(ctx: ToolContext, text: str) -> dict:
    view = ep.incident_view(ctx.g, ctx.iid)
    if view["status"] == "resolved" and not view["root_causes"]:
        text += "\n\nNote: resolved without a confirmed root cause."
        ctx.g.write("MATCH (i:Incident {id: $i}) SET i.unconfirmed_root_cause = true", {"i": ctx.iid})
    path = DATA_DIR / ctx.g.tenant_id / f"{ctx.iid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    ctx.g.write("MATCH (i:Incident {id: $i}) SET i.postmortem = $s, i.blob_ref = $ref",
                {"i": ctx.iid, "s": text[:SUMMARY_CHARS], "ref": f"{ctx.g.tenant_id}/{ctx.iid}.md"})
    return {"chars": len(text), "blob_ref": f"{ctx.g.tenant_id}/{ctx.iid}.md"}


def stub_postmortem(view: dict, report: dict) -> str:
    """Used when the LLM is unavailable: facts only, no prose."""
    cause = ", ".join(f"{r['label']} {r['id']}" for r in view["root_causes"]) or "not confirmed"
    fix = report.get("fault_class") or "none recorded"
    return (f"Incident {view['title']} ended {view['status']} ({view.get('reason') or 'no reason recorded'}). "
            f"Root cause: {cause}. Fix pattern: {fix}. Steps to resolve: {view.get('steps')}. "
            f"(Stub postmortem: written without the LLM.)")


for _t in [
    Tool("consolidate", "Turn the finished incident into memory: reinforce or create a playbook, write lessons.",
         {}, [], {SCRIBE}, _consolidate,
         lambda ctx, a, r: [("Playbook", r["playbook"], "playbook updated or created")] if r.get("playbook") else []),
    Tool("link_people", "Record who responded and who authored the cause.", {}, [], {SCRIBE}, _link_people),
    Tool("write_postmortem", "Store the postmortem text for this incident.", {"text": {"type": "string"}}, ["text"],
         {SCRIBE}, _write_postmortem),
]:
    register(_t)
