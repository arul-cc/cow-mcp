"""The contract for how an orchestration reports itself, and a place to keep what it reported.

The orchestrator calls two tools, run_plan once when it has a plan and run_step as each step moves. Both validate strictly and
refuse what does not fit, so the calling agent is told and corrects itself, instead of a client having to guess at prose. The same
shape is read by the web assistant (osc-assistant-web/web/src/lib/runLog.ts); change one and change the other.

Nothing here touches the platform. When RUN_REPORT_DIR is set, each plan is also appended to <dir>/<plan_id>.jsonl, which is what
lets a run that paused be picked up again with get_run. When it is not set the tools only validate and echo, and the conversation
is the record.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

STATUSES = ("started", "done", "reused", "failed", "skipped")
TERMINAL = ("done", "reused", "failed", "skipped")
# What a step is, which the screen draws differently: a control holds tests, a dependency feeds one control into another.
KINDS = ("app", "scope", "control", "rule", "link", "dependency", "action", "workflow", "report", "step")
JOINS = ("link", "dependency")
MATCHES = ("full", "partial", "new")

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_POSITION = re.compile(r"^\d+(\.\d+){0,8}$")
_lock = threading.Lock()


def _text(value: Any, field: str, limit: int, required: bool = True) -> str | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{field} is longer than {limit} characters")
    return value


def _id(value: Any, field: str, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"{field} must be 1 to 80 characters of letters, digits, _ . : or -")
    return value


def _link(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError(f"{field} must be a web link of at most 500 characters")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{field} must be an http or https link")
    return value


def clean_needs(needs: Any) -> list[dict]:
    """What the person has to do: each {what, url?, step_id?}."""
    if needs is None:
        return []
    if not isinstance(needs, list) or len(needs) > 20:
        raise ValueError("needs must be a list of at most 20 items")
    out = []
    for i, item in enumerate(needs):
        item = _as_dict(item, f"needs[{i}]")
        need = {"what": _text(item.get("what"), f"needs[{i}].what", 300)}
        if (url := _link(item.get("url"), f"needs[{i}].url")):
            need["url"] = url
        if (step_id := _id(item.get("step_id"), f"needs[{i}].step_id", required=False)):
            need["step_id"] = step_id
        out.append(need)
    return out


def _as_dict(value: Any, field: str) -> dict:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _step(raw: Any, i: int) -> dict:
    raw = _as_dict(raw, f"steps[{i}]")
    n = raw.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"steps[{i}].n must be a whole number from 1")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ValueError(f"steps[{i}].kind must be one of {', '.join(KINDS)}")
    step = {
        "id": _id(raw.get("id"), f"steps[{i}].id"),
        "n": n,
        "name": _text(raw.get("name"), f"steps[{i}].name", 200),
        "type": _text(raw.get("type"), f"steps[{i}].type", 80),
        "specialist": _text(raw.get("specialist"), f"steps[{i}].specialist", 80),
        "kind": kind,
        "scope": _text(raw.get("scope"), f"steps[{i}].scope", 40),
    }
    if (position := raw.get("position")) is not None:
        if not isinstance(position, str) or not _POSITION.match(position):
            raise ValueError(f"steps[{i}].position must be a control's place in its assessment such as 5, 5.6 or 5.6.1")
        step["position"] = position
    if (parent := _id(raw.get("parent"), f"steps[{i}].parent", required=False)):
        step["parent"] = parent
    if (details := _text(raw.get("details"), f"steps[{i}].details", 500, required=False)):
        step["details"] = details
    ends = raw.get("ends")
    if kind in JOINS:
        if ends is None:
            raise ValueError(f"steps[{i}] is a {kind}, so it needs ends: {{from_step, to_step}}, the two steps it joins")
        ends = _as_dict(ends, f"steps[{i}].ends")
        step["ends"] = {"from_step": _id(ends.get("from_step"), f"steps[{i}].ends.from_step"), "to_step": _id(ends.get("to_step"), f"steps[{i}].ends.to_step")}
    elif ends is not None:
        raise ValueError(f"steps[{i}].ends is only for a link or a dependency, not a {kind}")
    return step


def _check_references(steps: list[dict]) -> None:
    """Every step a step points at must be in the plan, and what hangs under what must not loop."""
    ids = {s["id"] for s in steps}
    parent_of = {}
    for s in steps:
        refs = [("parent", s["parent"])] if "parent" in s else []
        if "ends" in s:
            refs += [("ends.from_step", s["ends"]["from_step"]), ("ends.to_step", s["ends"]["to_step"])]
        for field, target in refs:
            if target not in ids:
                raise ValueError(f"step {s['id']!r}: {field} {target!r} is not a step of this plan")
            if target == s["id"]:
                raise ValueError(f"step {s['id']!r}: {field} points at itself")
        if "ends" in s and s["ends"]["from_step"] == s["ends"]["to_step"]:
            raise ValueError(f"step {s['id']!r}: a {s['kind']} joins two different steps")
        if "parent" in s:
            parent_of[s["id"]] = s["parent"]
    for start in parent_of:
        seen, at = set(), start
        while at in parent_of:
            if at in seen:
                raise ValueError(f"step {start!r}: parent steps loop back on themselves")
            seen.add(at)
            at = parent_of[at]


def clean_plan(plan_id: Any, title: Any, steps: Any, needs: Any = None, match: Any = None, match_note: Any = None, summary: Any = None, assumptions: Any = None) -> dict:
    plan_id = _id(plan_id, "plan_id")
    title = _text(title, "title", 200)
    if not isinstance(steps, list) or not 1 <= len(steps) <= 300:
        raise ValueError("steps must be a list of 1 to 300 steps")
    out, ids, numbers = [], set(), set()
    for i, raw in enumerate(steps):
        step = _step(raw, i)
        if step["id"] in ids or step["n"] in numbers:
            raise ValueError(f"steps[{i}] repeats an id or a number")
        ids.add(step["id"])
        numbers.add(step["n"])
        out.append(step)
    _check_references(out)
    plan = {"plan_id": plan_id, "title": title, "steps": sorted(out, key=lambda s: s["n"]), "needs": clean_needs(needs)}
    if match is not None:
        if match not in MATCHES:
            raise ValueError(f"match must be one of {', '.join(MATCHES)}")
        plan["match"] = match
    if (note := _text(match_note, "match_note", 500, required=False)):
        plan["match_note"] = note
    if (text := _text(summary, "summary", 1000, required=False)):
        plan["summary"] = text
    if assumptions is not None:
        if not isinstance(assumptions, list) or len(assumptions) > 30:
            raise ValueError("assumptions must be a list of at most 30 items")
        plan["assumptions"] = [_text(a, f"assumptions[{j}]", 400) for j, a in enumerate(assumptions)]
    return plan


def clean_step(plan_id: Any, step_id: Any, status: Any, summary: Any = None, object: Any = None, session: Any = None, needs: Any = None) -> dict:
    plan_id = _id(plan_id, "plan_id")
    step_id = _id(step_id, "step_id")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    event: dict[str, Any] = {"plan_id": plan_id, "step_id": step_id, "status": status, "needs": clean_needs(needs)}
    if (text := _text(summary, "summary", 2000, required=False)):
        event["summary"] = text
    if object is not None:
        made = _as_dict(object, "object")
        item = {"type": _text(made.get("type"), "object.type", 80), "name": _text(made.get("name"), "object.name", 200)}
        if (made_id := _id(made.get("id"), "object.id", required=False)):
            item["id"] = made_id
        if (url := _link(made.get("url"), "object.url")):
            item["url"] = url
        event["object"] = item
    if (sess := _id(session, "session", required=False)):
        event["session"] = sess
    return event


class RunStore:
    """One append-only file per plan. Optional: with no directory it keeps nothing."""

    def __init__(self, directory: str | None):
        self.directory = directory or ""

    @property
    def enabled(self) -> bool:
        return self.directory != ""

    def _path(self, plan_id: str) -> str:
        # plan_id has already been checked to be a plain id, so it cannot name a path
        return os.path.join(self.directory, f"{plan_id}.jsonl")

    def append(self, plan_id: str, record: dict) -> None:
        if not self.enabled:
            return
        os.makedirs(self.directory, exist_ok=True)
        line = json.dumps({**record, "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False)
        with _lock, open(self._path(plan_id), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read(self, plan_id: str) -> list[dict]:
        if not self.enabled or not os.path.exists(self._path(plan_id)):
            return []
        records = []
        with open(self._path(plan_id), encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
        return records


def state_of(records: list[dict]) -> dict | None:
    """The plan and each step's latest status, from what was stored. None when there was no plan."""
    plan = None
    status: dict[str, str] = {}
    for r in records:
        if r.get("kind") == "plan":
            plan, status = r["plan"], {}
        elif r.get("kind") == "step" and plan:
            status[r["event"]["step_id"]] = r["event"]["status"]
    if not plan:
        return None
    steps = [{"id": s["id"], "n": s["n"], "name": s["name"], "status": status.get(s["id"], "pending")} for s in plan["steps"]]
    nxt = next((s["id"] for s in steps if s["status"] not in TERMINAL), None)
    return {"plan_id": plan["plan_id"], "title": plan["title"], "steps": steps, "done": sum(1 for s in steps if s["status"] in TERMINAL), "total": len(steps), "next_step_id": nxt}


_MINTED = re.compile(r"-[0-9a-f]{8}$")


def mint_plan_id(name: str) -> str:
    """A plan id nobody else has. The agent only picks a short name for the plan, and the same name comes back every run, so the id
    that keys the stored run is the name with a random suffix: two chats can never write into one run, and a name that already
    carries a suffix is not given a second."""
    base = _MINTED.sub("", name)[:60]
    return f"{base}-{secrets.token_hex(4)}"


def record_plan(store: RunStore, plan: dict) -> dict:
    """Stores the plan under a fresh id and says which id every later call must use."""
    plan = {**plan, "plan_id": mint_plan_id(plan["plan_id"])}
    store.append(plan["plan_id"], {"kind": "plan", "plan": plan})
    return {"ok": True, "plan_id": plan["plan_id"], "steps": len(plan["steps"]), "persisted": store.enabled, "next": f"Use plan_id {plan['plan_id']!r} in every run_step and in get_run."}


def record_step(store: RunStore, event: dict) -> dict:
    out: dict[str, Any] = {"ok": True, "plan_id": event["plan_id"], "step_id": event["step_id"], "status": event["status"], "persisted": store.enabled}
    if store.enabled:
        state = state_of(store.read(event["plan_id"]))
        if state is None:
            raise ValueError(f"unknown plan_id {event['plan_id']!r}: call run_plan first")
        valid = [s["id"] for s in state["steps"]]
        if event["step_id"] not in valid:
            raise ValueError(f"step_id {event['step_id']!r} is not in the plan; its steps are {', '.join(valid[:12])}{'...' if len(valid) > 12 else ''}")
    store.append(event["plan_id"], {"kind": "step", "event": event})
    if store.enabled:
        state = state_of(store.read(event["plan_id"])) or {}
        out.update({"done": state.get("done"), "total": state.get("total"), "next_step_id": state.get("next_step_id")})
    return out


def get_run(store: RunStore, plan_id: Any) -> dict:
    plan_id = _id(plan_id, "plan_id")
    if not store.enabled:
        raise ValueError("runs are not being kept on this server")
    state = state_of(store.read(plan_id))
    if state is None:
        raise ValueError(f"no run for plan_id {plan_id!r}")
    return state
