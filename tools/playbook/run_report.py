"""The tools an orchestrator reports its run through. The rules are in run_report_contract.py."""
from __future__ import annotations

import os
from typing import Literal

from fastmcp import Context
from pydantic import BaseModel

from mcpconfig.config import mcp
from tools.playbook import run_report_contract as contract

# Optional. Set it to keep each plan's reports on disk, which lets a paused run be picked up again with get_run.
STORE = contract.RunStore(os.environ.get("RUN_REPORT_DIR", "").strip())


class Ends(BaseModel):
    from_step: str
    to_step: str


class PlannedStep(BaseModel):
    id: str
    n: int
    name: str
    type: str
    specialist: str
    kind: Literal["app", "scope", "control", "rule", "link", "dependency", "action", "workflow", "report", "step"]
    scope: str
    position: str | None = None
    parent: str | None = None
    ends: Ends | None = None
    details: str | None = None


class Need(BaseModel):
    what: str
    url: str | None = None
    step_id: str | None = None


class MadeObject(BaseModel):
    type: str
    name: str
    id: str | None = None
    url: str | None = None


@mcp.tool()
async def run_plan(
    plan_id: str,
    title: str,
    steps: list[PlannedStep],
    needs: list[Need] | None = None,
    match: Literal["full", "partial", "new"] | None = None,
    match_note: str | None = None,
    summary: str | None = None,
    assumptions: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Purpose:
        Present the implementation plan, as data. The person's screen draws the plan, its tree and its progress from this call
        alone and never reads your text, so everything it needs must be in here.

    When to call:
        Once, as soon as the plan is formed. This IS how you present the plan: do not write the plan table out again in your
        reply. Call it again only if the plan changes; you get a new id back.

    Inputs:
        plan_id: a short name you choose for this plan (letters, digits, _ . : -). It is only a name: the id that identifies the run
                 is returned to you, with a suffix that makes it unique.
        title: the plan's title.
        steps: every step of the plan, in order, each with
            id: e.g. "s1".
            n: its number in the plan, from 1.
            name, type (the step type, e.g. "Control Setup"), specialist (e.g. "Rules Specialist").
            kind: app | scope | control | rule | link | dependency | action | workflow | report | step. A "rule" is the automated
                  test that collects a control's evidence; a "link" joins a control to a control of another layer; a
                  "dependency" feeds one control's data into another.
            scope: the layer it belongs to: App, Application Scope, L1, L2 or L3.
            position: for a control, its place in its assessment, e.g. "5", "5.6" or "5.6.1".
            parent: the id of the step it sits under: a rule's control, or a sub-control's parent control.
            ends: for a link or a dependency only, {from_step, to_step}: the ids of the two steps it joins.
            details: one line of free text for the person, e.g. what it targets. It is only displayed, so put the structure in
                     the fields above, not in this line.
        needs: what the user will have to do themselves, if you already know, e.g. enter a credential in the platform:
               each {what, url?, step_id?}.
        match: how well the Playbook catalog matched the requirement: full | partial | new.
        match_note: one line on what is and is not matched.
        summary: the plan's executive summary, a sentence or two.
        assumptions: what the plan assumes or needs in place, one short item each.

    Returns:
        {ok: true, plan_id, ...}. USE THE plan_id THAT COMES BACK, exactly, in every run_step and in get_run; never the name you
        passed, and never an id from an earlier plan. An error means the plan did not hold together, for example a step pointing at
        a step that is not in the plan: read it, fix it and call again.
    """
    return contract.record_plan(STORE, contract.clean_plan(plan_id, title, steps, needs, match, match_note, summary, assumptions))


@mcp.tool()
async def run_step(
    plan_id: str,
    step_id: str,
    status: Literal["started", "done", "reused", "failed", "skipped"],
    summary: str | None = None,
    object: MadeObject | None = None,
    session: str | None = None,
    needs: list[Need] | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Purpose:
        Report that a step of the plan has started, finished, been reused, failed or been skipped. A step is not finished until you
        have reported it here.

    When to call:
        With status "started" right before you hand a step to a specialist.
        With "done", "reused" (it already existed and you used it), "failed" or "skipped" right after the specialist answers,
        before you move to the next step.

    Inputs:
        plan_id: the id run_plan returned. step_id: as in run_plan.
        status: started | done | reused | failed | skipped.
        summary: one or two sentences on what happened.
        object: what the step made or found: {type, name, id?, url?} where url is the platform link the specialist returned.
        session: the specialist's session id, from "Delegated to subagent <id>".
        needs: anything the user must now do because of this step, e.g. enter a credential: each {what, url?}.

    Returns:
        {ok: true, ...} and, when runs are kept, how many steps are done and which step comes next. An error means the shape
        was wrong or the step is not in the plan: read it, fix it and call again.
    """
    return contract.record_step(STORE, contract.clean_step(plan_id, step_id, status, summary, object, session, needs))


@mcp.tool()
async def get_run(plan_id: str, ctx: Context | None = None) -> dict:
    """
    Purpose:
        Read back how far a plan got: each step's latest status and which step is next.

    When to call:
        When you resume a run that paused or that you were asked to continue, before you decide where to carry on.

    Inputs:
        plan_id: the id run_plan returned for the plan you are resuming.

    Returns:
        {plan_id, title, steps: [{id, n, name, status}], done, total, next_step_id}, or an error when runs are not kept here or
        the plan is unknown.
    """
    return contract.get_run(STORE, plan_id)
