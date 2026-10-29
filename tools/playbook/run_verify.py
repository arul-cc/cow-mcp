"""Checks that what an orchestration reported is really on the platform, and says whether the run is finished.

run_step records what the orchestrator says it made. This module looks at the platform and says whether that is true, one check per
kind of step (a control, a rule, a link, ...), so a client that wants to know "is this run done" does not have to know any tool of
any specialist. finish_run adds the whole run up.

A check has three answers. True: the platform has it. False: the platform answered and it is not there, or not as reported. None:
it could not be checked, because the kind has no check yet, the report did not say enough, or the platform did not answer. None is
never turned into False: a platform that is down must not make a finished run look broken.

The platform is reached through a Reader, so the checks can be tested without one.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from tools.playbook import run_report_contract as contract

VERIFIED_KINDS = ("app", "scope", "control", "rule", "link", "dependency")


@dataclass
class Lookup:
    """What the platform said. found is True or False when it answered, None when it did not."""
    found: bool | None
    data: Any = None
    problem: str | None = None
    # True when asking again will not change the answer, as when the platform refuses the caller's credentials.
    final: bool = False


_STATUS = re.compile(r"status:?\s*(\d{3})")


def answer_of(response: Any, what: str) -> Lookup:
    """What a platform call said. Only a 404 means the thing is not there; any other failure means we do not know, and a platform that
    is down or refuses us must never make a finished run look broken."""
    if isinstance(response, str):
        return Lookup(None, problem=response[:200])
    if isinstance(response, dict) and "error" in response:
        text = str(response["error"])
        match = _STATUS.search(text)
        status = match.group(1) if match else ""
        if status == "404":
            return Lookup(False, problem=f"{what} was not found")
        return Lookup(None, problem=text[:200], final=status in ("401", "403"))
    return Lookup(True, data=response)


def first_item_of(response: Any, what: str) -> Lookup:
    """For the platform's list answers: found when the list has something in it, and the first thing is the data."""
    answer = answer_of(response, what)
    if answer.found is not True:
        return answer
    items = answer.data.get("items") if isinstance(answer.data, dict) else None
    if not isinstance(items, list):
        return Lookup(None, problem=f"the platform's answer about {what} was not a list")
    return Lookup(bool(items), data=items[0] if items else None)


def items_of(response: Any, what: str) -> Lookup:
    """As first_item_of, with every item as the data."""
    answer = answer_of(response, what)
    if answer.found is not True:
        return answer
    items = answer.data.get("items") if isinstance(answer.data, dict) else None
    if not isinstance(items, list):
        return Lookup(None, problem=f"the platform's answer about {what} was not a list")
    return Lookup(bool(items), data=items)


class Reader(Protocol):
    async def control(self, control_id: str) -> Lookup: ...
    async def scope(self, scope_id: str) -> Lookup: ...
    async def application(self, application_id: str) -> Lookup: ...
    async def links(self, source_plan_id: str, source_control_id: str, target_plan_id: str, target_control_id: str) -> Lookup: ...


@dataclass
class Facts:
    """What a check may look at: the step, what it reported, and what the other steps of the plan reported."""
    spec: dict
    object: dict | None
    objects: dict[str, dict] = field(default_factory=dict)
    specs: dict[str, dict] = field(default_factory=dict)


def verdict(verified: bool | None, problem: str | None = None, settled: bool = False, as_found: dict | None = None) -> dict:
    """settled marks a None that waiting will not change, because the report itself lacks what the check needs. as_found is what the
    platform itself calls the object, so a name or a type is taken from the platform and never from what a model wrote."""
    out: dict[str, Any] = {"verified": verified}
    if problem:
        out["problem"] = problem
    if settled:
        out["settled"] = True
    if as_found:
        out["as_found"] = as_found
    return out


def _unknown(lookup: Lookup, what: str) -> dict:
    return verdict(None, f"could not check {what}: {lookup.problem or 'the platform did not answer'}", settled=lookup.final)


def _parent(spec: dict, objects: dict[str, dict]) -> dict | None:
    return objects.get(spec.get("parent") or "")


def _ends(spec: dict, objects: dict[str, dict]) -> tuple[dict | None, dict | None]:
    ends = spec.get("ends") or {}
    return objects.get(ends.get("from_step") or ""), objects.get(ends.get("to_step") or "")


def _named(name: Any) -> dict | None:
    return {"name": name} if isinstance(name, str) and name.strip() else None


# Every check identifies an object by what the platform itself says: its id, the number it has in its assessment, what it sits under.
# Nothing is decided by comparing a name or a label a model wrote.


async def check_control(facts: Facts, reader: Reader) -> dict:
    made = facts.object
    if not made or not made.get("id"):
        return verdict(None, "the report does not name the control's id", settled=True)
    found = await reader.control(made["id"])
    if found.found is None:
        return _unknown(found, f"control {made['id']}")
    if not found.found:
        return verdict(False, f"control {made['id']} is not on the platform")
    control, as_found = found.data, _named(found.data.get("name"))
    position = facts.spec.get("position")
    if position and str(control.get("displayable") or "").strip() != position.strip():
        return verdict(False, f"control {made['id']} is number {control.get('displayable')!r} on the platform, and the plan puts it at {position!r}", as_found=as_found)
    parent = _parent(facts.spec, facts.objects)
    if parent and parent.get("id") and control.get("parentControlId") != parent["id"]:
        return verdict(False, f"control {made['id']} sits under control {control.get('parentControlId')!r} on the platform, and the plan puts it under {parent['id']!r}", as_found=as_found)
    return verdict(True, as_found=as_found)


async def check_rule(facts: Facts, reader: Reader) -> dict:
    """A rule step makes what its plan says it makes (`makes`): a rule attached to its control, or evidence written on it. Either is
    looked for on the control the step sits under, by id."""
    made = facts.object
    makes = facts.spec.get("makes")
    if makes not in contract.MAKES:
        return verdict(None, "the plan does not say whether this rule step makes a rule or evidence", settled=True)
    if not made or not made.get("id"):
        return verdict(None, f"the report does not name the {makes}'s id", settled=True)
    parent = _parent(facts.spec, facts.objects)
    if not parent or not parent.get("id"):
        return verdict(None, f"the {makes}'s control did not report an id, so it cannot be checked", settled=True)
    found = await reader.control(parent["id"])
    if found.found is None:
        return _unknown(found, f"control {parent['id']}")
    if not found.found:
        return verdict(False, f"the control {parent['id']} the {makes} belongs to is not on the platform")
    control = found.data

    if makes == "evidence":
        held = [e for e in (control.get("evidences") or []) if isinstance(e, dict)]
        match = next((e for e in held if e.get("id") == made["id"]), None)
        if match:
            return verdict(True, as_found=_named(match.get("name")))
        ids = ", ".join(repr(e.get("id")) for e in held[:5]) or "none"
        return verdict(False, f"control {parent['id']} has no evidence {made['id']}; the evidence it has: {ids}")

    attached = control.get("ruleId") or ""
    if attached == made["id"]:
        return verdict(True, as_found=_named((control.get("rule") or {}).get("name")))
    if not attached:
        return verdict(False, f"no rule is attached to control {parent['id']}")
    return verdict(False, f"control {parent['id']} has the rule {attached!r} attached, not {made['id']!r}")


async def check_link(facts: Facts, reader: Reader) -> dict:
    source, target = _ends(facts.spec, facts.objects)
    if not source or not source.get("id") or not target or not target.get("id"):
        return verdict(None, "the two controls the link joins did not both report an id", settled=True)
    ends = []
    for control_id in (source["id"], target["id"]):
        found = await reader.control(control_id)
        if found.found is None:
            return _unknown(found, f"control {control_id}")
        if not found.found:
            return verdict(False, f"control {control_id}, an end of the link, is not on the platform")
        ends.append(found.data)
    if not ends[0].get("planId") or not ends[1].get("planId"):
        return verdict(None, "the platform did not say which assessment a control is in")
    found = await reader.links(ends[0]["planId"], source["id"], ends[1]["planId"], target["id"])
    if found.found is None:
        return _unknown(found, "the link")
    items = found.data if isinstance(found.data, list) else []
    wanted = (facts.object or {}).get("id")
    if items and (not wanted or any(item.get("id") == wanted for item in items)):
        return verdict(True)
    if items:
        return verdict(False, f"the platform has a link between those controls, but not the one reported ({wanted})")
    return verdict(False, "there is no link between those two controls on the platform")


async def check_dependency(facts: Facts, reader: Reader) -> dict:
    source, target = _ends(facts.spec, facts.objects)
    if not source or not source.get("id") or not target or not target.get("id"):
        return verdict(None, "the two controls the dependency joins did not both report an id", settled=True)
    found = await reader.control(target["id"])
    if found.found is None:
        return _unknown(found, f"control {target['id']}")
    if not found.found:
        return verdict(False, f"control {target['id']}, the one that takes the data, is not on the platform")
    inputs = (found.data or {}).get("dependedInputs") or {}
    fed = any(isinstance(m, dict) and m.get("planControlId") == source["id"] for m in inputs.values())
    return verdict(True) if fed else verdict(False, f"control {target['id']} takes no input from control {source['id']}")


async def check_scope(facts: Facts, reader: Reader) -> dict:
    made = facts.object
    if not made or not made.get("id"):
        return verdict(None, "the report does not name the scope's id", settled=True)
    found = await reader.scope(made["id"])
    if found.found is None:
        return _unknown(found, f"scope {made['id']}")
    if not found.found:
        return verdict(False, f"application scope {made['id']} is not on the platform")
    return verdict(True, as_found=_named(found.data.get("name")))


async def check_app(facts: Facts, reader: Reader) -> dict:
    made = facts.object
    if not made or not made.get("id"):
        return verdict(None, "the report does not name the application's id", settled=True)
    found = await reader.application(made["id"])
    if found.found is None:
        return _unknown(found, f"application {made['id']}")
    if not found.found:
        return verdict(False, f"application {made['id']} is not on the platform")
    return verdict(True, as_found=_named(found.data.get("credentialName")))


CHECKS: dict[str, Callable[[Facts, Reader], Awaitable[dict]]] = {
    "app": check_app,
    "scope": check_scope,
    "control": check_control,
    "rule": check_rule,
    "link": check_link,
    "dependency": check_dependency,
}


async def verify_step(facts: Facts, reader: Reader, retries: int = 2, delay: float = 1.5) -> dict:
    """The check for the step's kind. What the platform says can lag a moment behind what was just made, and can fail for a moment,
    so a False or None is looked at again a few times before it is believed."""
    check = CHECKS.get(facts.spec["kind"])
    if check is None:
        return verdict(None, f"a {facts.spec['kind']} step has no check yet")
    result = verdict(None, "not checked")
    for attempt in range(retries + 1):
        try:
            result = await check(facts, reader)
        except Exception as e:  # a check that fails must not fail the run it is checking
            result = verdict(None, f"the check failed: {e}")
        if result["verified"] is True or result.pop("settled", False):
            break
        if attempt < retries:
            await asyncio.sleep(delay)
    return result


def facts_of(ledger: dict, step_id: str) -> Facts:
    specs = {s["spec"]["id"]: s["spec"] for s in ledger["steps"]}
    objects = {s["spec"]["id"]: s["object"] for s in ledger["steps"] if s["object"]}
    return Facts(spec=specs[step_id], object=objects.get(step_id), objects=objects, specs=specs)


async def report_step(store: contract.RunStore, event: dict, reader: Reader, retries: int = 2, delay: float = 1.5) -> dict:
    """run_step: record what was reported and, for a step that made or found something, check it. The report is never refused for
    what the check finds: the answer comes back with it, so the orchestrator can put it right, and finish_run holds the line."""
    out = contract.record_step(store, event)
    if not store.enabled or event["status"] not in ("done", "reused"):
        return out
    ledger = contract.ledger_of(store.read(event["plan_id"]))
    result = await verify_step(facts_of(ledger, event["step_id"]), reader, retries, delay)
    store.append(event["plan_id"], {"kind": "verify", "step_id": event["step_id"], "result": result})
    out["verified"] = result["verified"]
    if result.get("problem"):
        out["problem"] = result["problem"]
    if result.get("as_found"):
        out["platform"] = result["as_found"]
    return out


def _line(step: dict) -> str:
    return f"step {step['spec']['n']} ({step['spec']['name']})"


async def finish_run(store: contract.RunStore, plan_id: Any, reader: Reader, retries: int = 1, delay: float = 1.5, strict: bool | None = None, parallel: int = 6) -> dict:
    """Whether the run is complete, looking at the platform now, not at what was true when each step was reported."""
    plan_id = contract._id(plan_id, "plan_id")
    if not store.enabled:
        raise ValueError("runs are not being kept on this server")
    ledger = contract.ledger_of(store.read(plan_id))
    if ledger is None:
        raise ValueError(f"no run for plan_id {plan_id!r}")
    if strict is None:
        strict = os.environ.get("FINISH_RUN_STRICT", "").strip() in ("1", "true", "yes")

    gate = asyncio.Semaphore(parallel)

    async def look(step: dict) -> dict:
        async with gate:
            return await verify_step(facts_of(ledger, step["spec"]["id"]), reader, retries, delay)

    reported = [s for s in ledger["steps"] if s["status"] in ("done", "reused")]
    results = await asyncio.gather(*(look(s) for s in reported))
    for step, result in zip(reported, results):
        step["check"] = result
        store.append(plan_id, {"kind": "verify", "step_id": step["spec"]["id"], "result": result})

    steps = ledger["steps"]
    opened = [s for s in steps if s["status"] in ("pending", "started")]
    failed = [s for s in steps if s["status"] == "failed"]
    wrong = [s for s in reported if s["check"]["verified"] is False]
    unchecked = [s for s in reported if s["check"]["verified"] is None]
    checked = [s for s in reported if s["check"]["verified"] is True]
    complete = not opened and not failed and not wrong and not (strict and unchecked)

    needs = [dict(n) for n in ledger["plan"].get("needs", [])]
    for s in steps:
        needs += [{"step_id": s["spec"]["id"], **n} for n in s["needs"]]
    counts = {
        "total": len(steps),
        "done": sum(s["status"] == "done" for s in steps),
        "reused": sum(s["status"] == "reused" for s in steps),
        "skipped": sum(s["status"] == "skipped" for s in steps),
        "failed": len(failed), "open": len(opened),
        "verified": len(checked), "unverified": len(unchecked), "mismatched": len(wrong),
    }

    def brief(step: dict, **more: Any) -> dict:
        return {"id": step["spec"]["id"], "n": step["spec"]["n"], "name": step["spec"]["name"], **more}

    nxt = opened[0]["spec"]["id"] if opened else None
    out: dict[str, Any] = {
        "plan_id": plan_id, "complete": complete, "counts": counts,
        "open": [brief(s, status=s["status"]) for s in opened],
        "failed": [brief(s, summary=s["summary"]) for s in failed],
        "mismatched": [brief(s, problem=s["check"].get("problem")) for s in wrong],
        "unverified": [brief(s, kind=s["spec"]["kind"], problem=s["check"].get("problem")) for s in unchecked],
        "needs": needs, "next_step": nxt,
    }
    if complete:
        note = f" {len(unchecked)} could not be checked." if unchecked else ""
        out["message"] = f"All {counts['total']} steps are finished and {len(checked)} were found on the platform.{note}"
    else:
        parts = []
        if opened:
            parts.append(f"{len(opened)} not finished ({', '.join(_line(s) for s in opened[:6])}{'...' if len(opened) > 6 else ''})")
        if failed:
            parts.append(f"{len(failed)} failed ({', '.join(_line(s) for s in failed[:6])})")
        if wrong:
            parts.append(f"{len(wrong)} reported but not as reported on the platform ({'; '.join(f'{_line(s)}: {s['check'].get('problem')}' for s in wrong[:4])})")
        if strict and unchecked:
            parts.append(f"{len(unchecked)} could not be checked")
        out["message"] = (f"The run is not complete: {'; '.join(parts)}. Call get_run with plan_id {plan_id!r}, finish the steps that are open, "
                          f"redo the ones that are not on the platform, retry the failed ones or report why they cannot be done, then call finish_run again.")
    return out
