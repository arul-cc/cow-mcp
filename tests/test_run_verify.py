import asyncio
import os
import shutil
import tempfile
import unittest
import weakref
from unittest import mock

from tools.playbook import run_report_contract as c
from tools.playbook import run_verify as v
from tools.playbook.run_verify import Lookup


def step(i, kind, **more):
    base = {"id": f"s{i}", "n": i, "name": f"Step {i}", "type": "Setup", "specialist": "Specialist", "kind": kind, "scope": "L1"}
    return {**base, **more}


STEPS = [
    step(1, "app", scope="App"),
    step(2, "scope", scope="Application Scope"),
    step(3, "control", position="5"),
    step(4, "rule", parent="s3", makes="rule"),
    step(5, "control", scope="L2", position="5.6", parent="s3"),
    step(6, "link", ends={"from_step": "s3", "to_step": "s5"}),
    step(7, "dependency", ends={"from_step": "s3", "to_step": "s5"}),
    step(8, "action"),
]

OBJECTS = {
    "s1": {"type": "application", "name": "veza", "id": "app-1"},
    "s2": {"type": "application-scope", "name": "veza-scope", "id": "scope-1"},
    "s3": {"type": "control", "name": "Okta", "id": "ctl-3"},
    "s4": {"type": "rule", "name": "GetUsers", "id": "rule-4"},
    "s5": {"type": "control", "name": "MFA", "id": "ctl-5"},
    "s6": {"type": "control-link", "name": "Roll up", "id": "link-6"},
    "s7": {"type": "control-dependency", "name": "Feed"},
    "s8": {"type": "action", "name": "Notify"},
}


class FakeReader:
    """The platform. An answer can be a list, which is read one answer at a time and then repeats its last: how a new object is
    missing for a moment and then there."""

    def __init__(self, controls=None, scopes=None, apps=None, links=None, down=False, refused=False):
        self.controls, self.scopes, self.apps, self.links_ = controls or {}, scopes or {}, apps or {}, links or {}
        self.down = down
        self.refused = refused
        self.calls = []

    def _pick(self, table, key, what):
        self.calls.append((what, key))
        if self.refused:
            return Lookup(None, problem="Unexpected response status: 403", final=True)
        if self.down:
            return Lookup(None, problem="HTTP 502")
        answer = table.get(key)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        return Lookup(True, answer) if answer is not None else Lookup(False)

    async def control(self, control_id):
        return self._pick(self.controls, control_id, "control")

    async def scope(self, scope_id):
        return self._pick(self.scopes, scope_id, "scope")

    async def application(self, application_id):
        return self._pick(self.apps, application_id, "application")

    async def links(self, source_plan_id, source_control_id, target_plan_id, target_control_id):
        key = (source_control_id, target_control_id)
        self.calls.append(("links", key))
        if self.down:
            return Lookup(None, problem="HTTP 502")
        found = self.links_.get(key) or []
        return Lookup(bool(found), found)


def control3(**over):
    return {"id": "ctl-3", "name": "Okta", "displayable": "5", "planId": "plan-a", "ruleId": "rule-4", "rule": {"name": "GetUsers"}, **over}


def control5(**over):
    return {"id": "ctl-5", "name": "MFA", "displayable": "5.6", "parentControlId": "ctl-3", "planId": "plan-b",
            "dependedInputs": {"InputFile1": {"planControlId": "ctl-3", "outputName": "Out"}}, **over}


def healthy():
    return FakeReader(
        controls={"ctl-3": control3(), "ctl-5": control5()},
        scopes={"scope-1": {"id": "scope-1", "name": "veza-scope"}},
        apps={"app-1": {"id": "app-1"}},
        links={("ctl-3", "ctl-5"): [{"id": "link-6"}]},
    )


def facts(step_id, objects=None, spec=None):
    objects = OBJECTS if objects is None else objects
    specs = {s["id"]: s for s in STEPS}
    if spec:
        specs[step_id] = {**specs[step_id], **spec}
    return v.Facts(spec=specs[step_id], object=objects.get(step_id), objects=objects, specs=specs)


def run(coro):
    return asyncio.run(coro)


def check(step_id, reader, objects=None, retries=2, spec=None):
    return run(v.verify_step(facts(step_id, objects, spec), reader, retries=retries, delay=0))


class Controls(unittest.TestCase):
    def test_a_control_that_is_there_is_verified_and_the_platforms_name_comes_back(self):
        self.assertEqual(check("s3", healthy()), {"verified": True, "as_found": {"name": "Okta"}})

    def test_a_control_that_is_not_there_is_false_after_looking_again(self):
        reader = FakeReader()
        result = check("s3", reader)
        self.assertIs(result["verified"], False)
        self.assertIn("ctl-3", result["problem"])
        self.assertEqual(len(reader.calls), 3, "one look and two more, in case the platform is a moment behind")

    def test_a_control_that_appears_a_moment_later_is_verified(self):
        reader = FakeReader(controls={"ctl-3": [None, control3()]})
        self.assertIs(check("s3", reader)["verified"], True)
        self.assertEqual(len(reader.calls), 2)

    def test_what_a_model_calls_the_control_does_not_matter_and_the_platforms_name_is_returned(self):
        objects = {**OBJECTS, "s3": {"type": "L2 Control", "name": "Whatever the specialist wrote", "id": "ctl-3"}}
        self.assertEqual(check("s3", healthy(), objects=objects), {"verified": True, "as_found": {"name": "Okta"}})

    def test_a_control_at_another_number_than_the_plan_puts_it_is_false(self):
        reader = FakeReader(controls={"ctl-3": control3(displayable="7")})
        result = check("s3", reader)
        self.assertIs(result["verified"], False)
        self.assertIn("'7'", result["problem"])
        self.assertIn("'5'", result["problem"])

    def test_the_number_is_compared_exactly_apart_from_surrounding_space(self):
        self.assertIs(check("s3", FakeReader(controls={"ctl-3": control3(displayable=" 5 ")}))["verified"], True)
        self.assertIs(check("s3", FakeReader(controls={"ctl-3": control3(displayable="05")}))["verified"], False)
        self.assertIs(check("s3", FakeReader(controls={"ctl-3": control3(displayable="5.0")}))["verified"], False)

    def test_a_number_that_is_only_part_of_the_planned_number_is_false(self):
        """5.6 is the control above 5.6.1, not the same control; 6.1 is part of 5.6.1 and is another control altogether."""
        for on_platform in ("5.6", "6.1", "5", "5.6.1.2"):
            reader = FakeReader(controls={"ctl-3": control3(displayable=on_platform)})
            self.assertIs(check("s3", reader, spec={"position": "5.6.1"})["verified"], False, on_platform)
        self.assertIs(check("s3", FakeReader(controls={"ctl-3": control3(displayable="5.6.1")}), spec={"position": "5.6.1"})["verified"], True)

    def test_a_plan_that_gives_no_number_does_not_compare_one(self):
        reader = FakeReader(controls={"ctl-3": control3(displayable="anything")})
        self.assertIs(check("s3", reader, spec={"position": None})["verified"], True)

    def test_a_control_under_another_control_than_the_plan_puts_it_is_false(self):
        reader = healthy()
        reader.controls["ctl-5"] = control5(parentControlId="ctl-9")
        result = check("s5", reader)
        self.assertIs(result["verified"], False)
        self.assertIn("ctl-9", result["problem"])
        self.assertIn("ctl-3", result["problem"])

    def test_a_control_with_no_control_above_it_on_the_platform_is_false_when_the_plan_has_one(self):
        reader = healthy()
        reader.controls["ctl-5"] = control5(parentControlId=None)
        self.assertIs(check("s5", reader)["verified"], False)

    def test_a_parent_that_did_not_report_an_id_is_not_compared(self):
        objects = {**OBJECTS, "s3": {"type": "control", "name": "Okta"}}
        self.assertIs(check("s5", healthy(), objects=objects)["verified"], True)

    def test_a_platform_that_does_not_answer_is_unknown_never_false(self):
        result = check("s3", FakeReader(down=True))
        self.assertIs(result["verified"], None)
        self.assertIn("HTTP 502", result["problem"])

    def test_a_platform_that_refuses_our_credentials_is_not_asked_again(self):
        reader = FakeReader(refused=True)
        result = check("s3", reader, retries=5)
        self.assertIs(result["verified"], None)
        self.assertIn("403", result["problem"])
        self.assertEqual(len(reader.calls), 1, "a refusal will not change by waiting")
        self.assertNotIn("settled", result)

    def test_a_report_without_an_id_is_unknown_and_is_not_looked_at_again(self):
        reader = healthy()
        result = check("s3", reader, objects={**OBJECTS, "s3": {"type": "control", "name": "Okta"}}, retries=5)
        self.assertIs(result["verified"], None)
        self.assertEqual(reader.calls, [], "nothing to look up, and waiting will not change that")
        self.assertNotIn("settled", result)

    def test_a_step_with_no_report_is_unknown(self):
        self.assertIs(check("s3", healthy(), objects={})["verified"], None)


class Rules(unittest.TestCase):
    def test_a_rule_attached_by_id_is_verified(self):
        self.assertEqual(check("s4", healthy()), {"verified": True, "as_found": {"name": "GetUsers"}})

    def test_a_rule_that_was_renamed_is_still_the_one_attached_and_its_platform_name_comes_back(self):
        reader = FakeReader(controls={"ctl-3": control3(rule={"name": "Renamed since"})})
        self.assertEqual(check("s4", reader), {"verified": True, "as_found": {"name": "Renamed since"}})

    def test_the_name_does_not_decide_which_rule_is_attached(self):
        reader = FakeReader(controls={"ctl-3": control3(ruleId="other", rule={"name": "GetUsers"})})
        result = check("s4", reader)
        self.assertIs(result["verified"], False)
        self.assertIn("'other'", result["problem"])

    def test_no_rule_on_the_control_is_false(self):
        result = check("s4", FakeReader(controls={"ctl-3": control3(ruleId=None, rule=None)}))
        self.assertIs(result["verified"], False)
        self.assertIn("no rule is attached", result["problem"])

    def test_a_control_that_is_gone_makes_the_rule_false(self):
        self.assertIs(check("s4", FakeReader())["verified"], False)

    def test_a_rule_whose_control_has_no_id_is_unknown(self):
        objects = {**OBJECTS, "s3": {"type": "control", "name": "Okta"}}
        self.assertIs(check("s4", healthy(), objects=objects)["verified"], None)

    def test_a_rule_that_reported_no_id_is_unknown_and_nothing_is_looked_up(self):
        reader = healthy()
        result = check("s4", reader, objects={**OBJECTS, "s4": {"type": "rule", "name": "GetUsers"}})
        self.assertIs(result["verified"], None)
        self.assertIn("rule's id", result["problem"])
        self.assertEqual(reader.calls, [])

    def test_a_plan_that_does_not_say_what_a_rule_step_makes_is_unknown_not_wrong(self):
        """A plan stored before the plan said it. Guessing rule would call evidence missing."""
        reader = healthy()
        result = check("s4", reader, spec={"makes": None})
        self.assertIs(result["verified"], None)
        self.assertIn("does not say", result["problem"])
        self.assertEqual(reader.calls, [])
        self.assertIs(check("s4", reader, spec={"makes": "rule "})["verified"], None)

    def test_what_the_specialist_called_the_object_does_not_matter(self):
        for label in ("rule", "L1 Rule", "banana", ""):
            objects = {**OBJECTS, "s4": {"type": label, "name": "x", "id": "rule-4"}}
            self.assertIs(check("s4", healthy(), objects=objects)["verified"], True, label)


EVIDENCE_CONTROL = control3(ruleId=None, rule=None, evidences=[
    {"id": "ev-1", "name": "Identify Non-Compliant Okta Applications Missing MFA"},
    {"id": "ev-2", "name": "OktaMFASupportingEvidence"},
])
MAKES_EVIDENCE = {"makes": "evidence"}


class Evidence(unittest.TestCase):
    """A rule step whose plan says it makes evidence: SQL evidence on an L2 or L3 control, not an attached rule. From a real run."""

    objects = {**OBJECTS, "s4": {"type": "L3 Evidence", "name": "OktaMFASupportingEvidence", "id": "ev-2"}}

    def check(self, reader, objects=None, spec=MAKES_EVIDENCE):
        return check("s4", reader, objects=self.objects if objects is None else objects, spec=spec)

    def test_evidence_that_is_on_the_control_is_verified_and_the_platforms_name_comes_back(self):
        result = self.check(FakeReader(controls={"ctl-3": EVIDENCE_CONTROL}))
        self.assertEqual(result, {"verified": True, "as_found": {"name": "OktaMFASupportingEvidence"}})

    def test_evidence_does_not_need_a_rule_to_be_attached(self):
        self.assertNotIn("ruleId", {k: v for k, v in EVIDENCE_CONTROL.items() if v})
        self.assertIs(self.check(FakeReader(controls={"ctl-3": EVIDENCE_CONTROL}))["verified"], True)

    def test_evidence_that_is_not_on_the_control_is_false_and_says_which_is(self):
        reader = FakeReader(controls={"ctl-3": {**EVIDENCE_CONTROL, "evidences": [{"id": "ev-1", "name": "Another"}]}})
        result = self.check(reader)
        self.assertIs(result["verified"], False)
        self.assertIn("ev-2", result["problem"])
        self.assertIn("ev-1", result["problem"])

    def test_evidence_is_found_by_its_id_not_its_name(self):
        reader = FakeReader(controls={"ctl-3": {**EVIDENCE_CONTROL, "evidences": [{"id": "ev-9", "name": "OktaMFASupportingEvidence"}]}})
        self.assertIs(self.check(reader)["verified"], False)

    def test_evidence_with_no_id_is_unknown(self):
        objects = {**self.objects, "s4": {"type": "evidence", "name": "OktaMFASupportingEvidence"}}
        reader = FakeReader(controls={"ctl-3": EVIDENCE_CONTROL})
        result = self.check(reader, objects=objects)
        self.assertIs(result["verified"], None)
        self.assertIn("evidence's id", result["problem"])
        self.assertEqual(reader.calls, [])

    def test_evidence_whose_control_is_gone_is_false(self):
        self.assertIs(self.check(FakeReader())["verified"], False)

    def test_it_is_the_plan_that_says_what_the_step_makes_not_the_label(self):
        reader = FakeReader(controls={"ctl-3": EVIDENCE_CONTROL})
        for label in ("L3 Evidence", "rule", "banana", ""):
            objects = {**self.objects, "s4": {"type": label, "name": "x", "id": "ev-2"}}
            self.assertIs(self.check(reader, objects=objects)["verified"], True, label)
        as_rule = self.check(reader, spec={"makes": "rule"})
        self.assertIs(as_rule["verified"], False, "the same object is checked as a rule when the plan says it makes a rule")
        self.assertIn("no rule is attached", as_rule["problem"])


class Links(unittest.TestCase):
    def test_the_reported_link_is_verified(self):
        self.assertEqual(check("s6", healthy()), {"verified": True})

    def test_a_link_between_the_controls_that_is_not_the_reported_one_is_false(self):
        reader = healthy()
        reader.links_[("ctl-3", "ctl-5")] = [{"id": "another-link"}]
        result = check("s6", reader)
        self.assertIs(result["verified"], False)
        self.assertIn("link-6", result["problem"])

    def test_no_link_is_false(self):
        reader = healthy()
        reader.links_.clear()
        self.assertIs(check("s6", reader)["verified"], False)

    def test_a_link_with_no_reported_id_is_verified_by_the_controls_alone(self):
        objects = {**OBJECTS, "s6": {"type": "control-link", "name": "Roll up"}}
        self.assertEqual(check("s6", healthy(), objects=objects), {"verified": True})

    def test_an_end_that_is_gone_is_false(self):
        reader = healthy()
        del reader.controls["ctl-5"]
        result = check("s6", reader)
        self.assertIs(result["verified"], False)
        self.assertIn("ctl-5", result["problem"])

    def test_an_end_without_an_id_is_unknown(self):
        objects = {**OBJECTS, "s5": {"type": "control", "name": "MFA"}}
        self.assertIs(check("s6", healthy(), objects=objects)["verified"], None)

    def test_a_control_that_does_not_say_its_assessment_is_unknown(self):
        reader = healthy()
        reader.controls["ctl-3"] = {"id": "ctl-3", "name": "Okta"}
        self.assertIs(check("s6", reader)["verified"], None)

    def test_the_links_are_looked_up_in_the_direction_reported(self):
        reader = healthy()
        check("s6", reader)
        self.assertIn(("links", ("ctl-3", "ctl-5")), reader.calls)


class Dependencies(unittest.TestCase):
    def test_a_control_fed_by_the_other_is_verified(self):
        self.assertEqual(check("s7", healthy()), {"verified": True})

    def test_a_control_fed_by_something_else_is_false(self):
        reader = healthy()
        reader.controls["ctl-5"]["dependedInputs"] = {"InputFile1": {"planControlId": "ctl-99", "outputName": "Out"}}
        result = check("s7", reader)
        self.assertIs(result["verified"], False)
        self.assertIn("ctl-3", result["problem"])

    def test_a_control_that_takes_no_input_is_false(self):
        reader = healthy()
        del reader.controls["ctl-5"]["dependedInputs"]
        self.assertIs(check("s7", reader)["verified"], False)


class ScopesAndApplications(unittest.TestCase):
    def test_a_scope_is_verified_and_the_platforms_name_comes_back(self):
        self.assertEqual(check("s2", healthy()), {"verified": True, "as_found": {"name": "veza-scope"}})

    def test_a_missing_scope_is_false(self):
        self.assertIs(check("s2", FakeReader())["verified"], False)

    def test_what_the_scope_is_called_on_the_platform_does_not_decide(self):
        reader = FakeReader(scopes={"scope-1": {"id": "scope-1", "name": "other"}})
        self.assertEqual(check("s2", reader), {"verified": True, "as_found": {"name": "other"}})

    def test_an_application_is_verified(self):
        self.assertIs(check("s1", healthy())["verified"], True)

    def test_a_missing_application_is_false(self):
        self.assertIs(check("s1", FakeReader())["verified"], False)

    def test_an_application_is_not_compared_by_name_and_its_platform_name_comes_back(self):
        reader = FakeReader(apps={"app-1": {"id": "app-1", "credentialName": "a different label"}})
        self.assertEqual(check("s1", reader), {"verified": True, "as_found": {"name": "a different label"}})


class Everything(unittest.TestCase):
    def test_a_kind_with_no_check_is_unknown_and_nothing_is_looked_up(self):
        reader = healthy()
        result = check("s8", reader)
        self.assertIs(result["verified"], None)
        self.assertIn("action", result["problem"])
        self.assertEqual(reader.calls, [])

    def test_a_check_that_breaks_is_unknown_not_an_error(self):
        class Broken(FakeReader):
            async def control(self, control_id):
                raise RuntimeError("boom")

        result = check("s3", Broken())
        self.assertIs(result["verified"], None)
        self.assertIn("boom", result["problem"])

    def test_every_kind_the_plan_can_hold_that_can_be_checked_has_a_check(self):
        self.assertEqual(set(v.CHECKS), set(v.VERIFIED_KINDS))
        self.assertTrue(set(v.VERIFIED_KINDS) <= set(c.KINDS))


class WhatThePlatformSays(unittest.TestCase):
    def test_only_a_404_means_it_is_not_there(self):
        gone = v.answer_of({"error": "Unexpected response status: 404"}, "control x")
        self.assertIs(gone.found, False)
        for other in ("Unexpected response status: 403", "Unexpected response status: 500", "Unexpected response status: 502", "Facing error : Request timed out."):
            self.assertIsNone(v.answer_of({"error": other}, "control x").found, other)

    def test_a_refusal_is_final_and_other_failures_are_not(self):
        for refused in ("Unexpected response status: 401", "Unexpected response status: 403"):
            self.assertTrue(v.answer_of({"error": refused}, "x").final, refused)
        for other in ("Unexpected response status: 500", "Unexpected response status: 502", "Facing error : Request timed out."):
            self.assertFalse(v.answer_of({"error": other}, "x").final, other)

    def test_a_text_answer_is_a_failure_to_answer(self):
        self.assertIsNone(v.answer_of("Facing error  :  boom", "control x").found)

    def test_an_answer_is_what_was_found(self):
        found = v.answer_of({"id": "c1"}, "control c1")
        self.assertIs(found.found, True)
        self.assertEqual(found.data, {"id": "c1"})

    def test_a_list_with_something_in_it_is_found_and_an_empty_one_is_not(self):
        self.assertEqual(v.first_item_of({"items": [{"id": "a"}, {"id": "b"}]}, "x").data, {"id": "a"})
        self.assertIs(v.first_item_of({"items": []}, "x").found, False)
        self.assertEqual(v.items_of({"items": [{"id": "a"}, {"id": "b"}]}, "x").data, [{"id": "a"}, {"id": "b"}])
        self.assertIs(v.items_of({"items": []}, "x").found, False)

    def test_a_list_answer_that_is_not_a_list_is_unknown(self):
        self.assertIsNone(v.first_item_of({"items": "oops"}, "x").found)
        self.assertIsNone(v.items_of({"nothing": 1}, "x").found)

    def test_a_failure_in_a_list_answer_is_not_turned_into_not_found(self):
        self.assertIsNone(v.first_item_of({"error": "Unexpected response status: 403"}, "x").found)
        self.assertIsNone(v.items_of({"error": "Unexpected response status: 502"}, "x").found)


class Ledger:
    """A run kept on disk, built up the way the orchestrator does it."""

    def __init__(self, reader, steps=STEPS):
        self.dir = tempfile.mkdtemp()
        weakref.finalize(self, shutil.rmtree, self.dir, True)
        self.store = c.RunStore(self.dir)
        self.reader = reader
        plan = c.clean_plan("p", "Plan", steps)
        self.plan_id = c.record_plan(self.store, plan)["plan_id"]

    def report(self, step_id, status="done", object=None, **more):
        event = c.clean_step(self.plan_id, step_id, status, object=object if object is not None else OBJECTS.get(step_id), **more)
        return run(v.report_step(self.store, event, self.reader, retries=1, delay=0))

    def everything(self, skip=()):
        for s in STEPS:
            if s["id"] not in skip:
                self.report(s["id"])

    def finish(self, **more):
        return run(v.finish_run(self.store, self.plan_id, self.reader, retries=1, delay=0, **more))


class ReportingAStep(unittest.TestCase):
    def test_a_finished_step_comes_back_with_what_the_platform_says(self):
        ledger = Ledger(healthy())
        out = ledger.report("s3")
        self.assertTrue(out["ok"])
        self.assertIs(out["verified"], True)
        self.assertNotIn("problem", out)

    def test_the_answer_carries_the_name_the_platform_gives_the_object(self):
        out = Ledger(healthy()).report("s3", object={"type": "L2 Control", "name": "What the specialist wrote", "id": "ctl-3"})
        self.assertIs(out["verified"], True)
        self.assertEqual(out["platform"], {"name": "Okta"})

    def test_an_object_that_is_not_there_has_no_platform_name(self):
        self.assertNotIn("platform", Ledger(FakeReader()).report("s3"))

    def test_a_report_the_platform_contradicts_is_kept_and_not_refused(self):
        ledger = Ledger(FakeReader())
        out = ledger.report("s3")
        self.assertTrue(out["ok"])
        self.assertIs(out["verified"], False)
        self.assertIn("ctl-3", out["problem"])
        self.assertEqual(c.state_of(ledger.store.read(ledger.plan_id))["steps"][2]["status"], "done")

    def test_a_step_that_has_only_started_is_not_looked_at(self):
        ledger = Ledger(healthy())
        out = ledger.report("s3", status="started", object={"type": "control", "name": "Okta", "id": "ctl-3"})
        self.assertNotIn("verified", out)
        self.assertEqual(ledger.reader.calls, [])

    def test_a_failed_step_is_not_looked_at(self):
        ledger = Ledger(healthy())
        self.assertNotIn("verified", ledger.report("s3", status="failed"))
        self.assertEqual(ledger.reader.calls, [])

    def test_a_reused_step_is_looked_at(self):
        self.assertIs(Ledger(healthy()).report("s3", status="reused")["verified"], True)

    def test_without_a_kept_run_nothing_is_checked_and_the_report_is_still_accepted(self):
        reader = healthy()
        out = run(v.report_step(c.RunStore(""), c.clean_step("p", "s3", "done", object=OBJECTS["s3"]), reader))
        self.assertTrue(out["ok"])
        self.assertNotIn("verified", out)
        self.assertEqual(reader.calls, [])

    def test_an_unknown_step_is_still_refused(self):
        ledger = Ledger(healthy())
        with self.assertRaises(ValueError):
            ledger.report("s99")

    def test_get_run_shows_which_steps_the_platform_did_not_have(self):
        ledger = Ledger(FakeReader())
        ledger.report("s3")
        steps = c.get_run(ledger.store, ledger.plan_id)["steps"]
        self.assertIs(steps[2]["verified"], False)
        self.assertNotIn("verified", steps[0], "a step nobody has checked says nothing")

    def test_reporting_a_step_again_drops_the_check_of_the_old_report(self):
        ledger = Ledger(FakeReader())
        ledger.report("s3")
        ledger.report("s3", status="started", object={"type": "control", "name": "Okta", "id": "ctl-3"})
        self.assertNotIn("verified", c.get_run(ledger.store, ledger.plan_id)["steps"][2])


class FinishingARun(unittest.TestCase):
    def test_a_run_whose_steps_are_all_there_is_complete(self):
        ledger = Ledger(healthy())
        ledger.everything()
        out = ledger.finish()
        self.assertTrue(out["complete"], out["message"])
        self.assertEqual(out["counts"], {"total": 8, "done": 8, "reused": 0, "skipped": 0, "failed": 0, "open": 0, "verified": 7, "unverified": 1, "mismatched": 0})
        self.assertEqual([u["id"] for u in out["unverified"]], ["s8"])
        self.assertIsNone(out["next_step"])
        self.assertIn("8 steps", out["message"])

    def test_a_step_that_was_never_reported_is_open(self):
        ledger = Ledger(healthy())
        ledger.everything(skip=("s5", "s6"))
        out = ledger.finish()
        self.assertFalse(out["complete"])
        self.assertEqual([o["id"] for o in out["open"]], ["s5", "s6"])
        self.assertEqual(out["next_step"], "s5")
        self.assertEqual(out["counts"]["open"], 2)
        self.assertIn("get_run", out["message"])
        self.assertIn(ledger.plan_id, out["message"])

    def test_a_step_that_has_started_and_not_ended_is_open(self):
        ledger = Ledger(healthy())
        ledger.everything(skip=("s5",))
        ledger.report("s5", status="started")
        out = ledger.finish()
        self.assertEqual([(o["id"], o["status"]) for o in out["open"] if o["id"] == "s5"], [("s5", "started")])

    def test_a_failed_step_stops_the_run_being_complete(self):
        ledger = Ledger(healthy())
        ledger.everything(skip=("s8",))
        ledger.report("s8", status="failed", summary="the API said no")
        out = ledger.finish()
        self.assertFalse(out["complete"])
        self.assertEqual(out["failed"], [{"id": "s8", "n": 8, "name": "Step 8", "summary": "the API said no"}])

    def test_a_skipped_step_does_not(self):
        ledger = Ledger(healthy())
        ledger.everything(skip=("s8",))
        ledger.report("s8", status="skipped")
        out = ledger.finish()
        self.assertTrue(out["complete"], out["message"])
        self.assertEqual(out["counts"]["skipped"], 1)

    def test_a_reused_step_that_has_gone_is_caught_too(self):
        reader = healthy()
        ledger = Ledger(reader)
        ledger.everything(skip=("s3",))
        ledger.report("s3", status="reused")
        del reader.controls["ctl-3"]
        out = ledger.finish()
        self.assertFalse(out["complete"])
        self.assertIn("s3", {m["id"] for m in out["mismatched"]})
        self.assertEqual(out["counts"]["reused"], 1)

    def test_a_step_the_platform_does_not_have_stops_the_run_being_complete(self):
        reader = healthy()
        ledger = Ledger(reader)
        ledger.everything()
        del reader.controls["ctl-5"]
        out = ledger.finish()
        self.assertFalse(out["complete"])
        self.assertEqual({m["id"] for m in out["mismatched"]}, {"s5", "s6", "s7"})
        self.assertIn("ctl-5", out["mismatched"][0]["problem"])
        self.assertIn("not on the platform", out["message"])

    def test_it_looks_at_the_platform_now_not_when_the_step_was_reported(self):
        reader = healthy()
        ledger = Ledger(reader)
        self.assertIs(ledger.report("s3")["verified"], True)
        ledger.everything(skip=("s3",))
        self.assertTrue(ledger.finish()["complete"])
        del reader.controls["ctl-3"]
        out = ledger.finish()
        self.assertFalse(out["complete"], "the control was removed after it was reported and checked")
        self.assertIn("s3", {m["id"] for m in out["mismatched"]})

    def test_a_platform_that_is_down_does_not_make_a_finished_run_look_broken(self):
        ledger = Ledger(FakeReader(down=True))
        ledger.everything()
        out = ledger.finish()
        self.assertTrue(out["complete"], out["message"])
        self.assertEqual(out["counts"]["unverified"], 8)
        self.assertEqual(out["counts"]["mismatched"], 0)

    def test_strict_mode_does_not_accept_what_could_not_be_checked(self):
        ledger = Ledger(FakeReader(down=True))
        ledger.everything()
        out = ledger.finish(strict=True)
        self.assertFalse(out["complete"])
        self.assertIn("could not be checked", out["message"])

    def test_strict_mode_can_be_set_in_the_environment(self):
        ledger = Ledger(FakeReader(down=True))
        ledger.everything()
        with mock.patch.dict(os.environ, {"FINISH_RUN_STRICT": "1"}):
            self.assertFalse(ledger.finish()["complete"])
        with mock.patch.dict(os.environ, {"FINISH_RUN_STRICT": ""}):
            self.assertTrue(ledger.finish()["complete"])

    def test_the_kind_that_has_no_check_never_blocks_by_itself_unless_strict(self):
        ledger = Ledger(healthy())
        ledger.everything()
        self.assertTrue(ledger.finish()["complete"])
        self.assertFalse(ledger.finish(strict=True)["complete"])

    def test_what_the_user_still_has_to_do_is_collected(self):
        ledger = Ledger(healthy())
        ledger.everything(skip=("s1",))
        ledger.report("s1", status="failed", needs=[{"what": "Add the Veza credential", "url": "https://x.example/a"}])
        out = ledger.finish()
        self.assertEqual(out["needs"], [{"step_id": "s1", "what": "Add the Veza credential", "url": "https://x.example/a"}])

    def test_it_changes_nothing_that_a_second_call_would_see_differently(self):
        ledger = Ledger(healthy())
        ledger.everything()
        first, second = ledger.finish(), ledger.finish()
        self.assertEqual(first, second)
        self.assertEqual(c.state_of(ledger.store.read(ledger.plan_id))["done"], 8)

    def test_the_steps_are_looked_at_in_parallel_but_all_of_them_are_looked_at(self):
        reader = healthy()
        ledger = Ledger(reader)
        ledger.everything()
        reader.calls.clear()
        ledger.finish(parallel=2)
        self.assertEqual(sum(1 for what, _ in reader.calls if what == "scope"), 1)
        self.assertEqual(sum(1 for what, _ in reader.calls if what == "application"), 1)

    def test_an_unknown_plan_is_an_error(self):
        with self.assertRaises(ValueError):
            run(v.finish_run(c.RunStore(tempfile.mkdtemp()), "nope", healthy()))

    def test_runs_that_are_not_kept_cannot_be_finished(self):
        with self.assertRaises(ValueError):
            run(v.finish_run(c.RunStore(""), "p", healthy()))

    def test_a_new_plan_starts_the_run_over(self):
        ledger = Ledger(healthy())
        ledger.everything()
        ledger.store.append(ledger.plan_id, {"kind": "plan", "plan": c.clean_plan(ledger.plan_id, "Again", STEPS[:2])})
        out = ledger.finish()
        self.assertEqual(out["counts"]["total"], 2)
        self.assertEqual(out["counts"]["open"], 2)


class TheLedger(unittest.TestCase):
    def test_a_check_for_a_step_nobody_reported_is_ignored(self):
        plan = c.clean_plan("p", "Plan", STEPS)
        records = [{"kind": "plan", "plan": plan}, {"kind": "verify", "step_id": "s3", "result": {"verified": True}}]
        self.assertIsNone(c.ledger_of(records)["steps"][2]["check"])

    def test_the_last_object_a_step_named_is_kept_when_it_is_reported_again_without_one(self):
        plan = c.clean_plan("p", "Plan", STEPS)
        records = [
            {"kind": "plan", "plan": plan},
            {"kind": "step", "event": c.clean_step("p", "s3", "done", object=OBJECTS["s3"])},
            {"kind": "step", "event": c.clean_step("p", "s3", "started")},
        ]
        s3 = c.ledger_of(records)["steps"][2]
        self.assertEqual(s3["status"], "started")
        self.assertEqual(s3["object"], OBJECTS["s3"])


if __name__ == "__main__":
    unittest.main()
