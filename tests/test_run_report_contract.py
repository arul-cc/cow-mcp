import json
import os
import tempfile
import unittest

from tools.playbook import run_report_contract as c

STEPS = [{"id": "s1", "n": 1, "name": "Connect Veza", "type": "Application Setup", "specialist": "Application Specialist", "kind": "app", "scope": "App"},
         {"id": "s2", "n": 2, "name": "Okta Users", "type": "Control Setup", "specialist": "Rules Specialist", "kind": "control", "scope": "L1", "position": "2.1"}]


def plan(**over):
    args = {"plan_id": "p1", "title": "Enforce MFA", "steps": STEPS, "needs": [{"what": "Add the Veza credential", "url": "https://x.example/a", "step_id": "s1"}]}
    args.update(over)
    return c.clean_plan(**args)


class CleanPlan(unittest.TestCase):
    def test_reads_a_plan_and_orders_its_steps(self):
        p = plan(steps=list(reversed(STEPS)))
        self.assertEqual([s["n"] for s in p["steps"]], [1, 2])
        self.assertEqual(p["needs"], [{"what": "Add the Veza credential", "url": "https://x.example/a", "step_id": "s1"}])

    def test_refuses_what_does_not_fit(self):
        bad = [
            dict(plan_id="has space"), dict(plan_id=""), dict(title=""), dict(steps=[]), dict(steps="x"),
            dict(steps=[{**STEPS[0]}, {**STEPS[1], "id": "s1"}]),
            dict(steps=[{**STEPS[0]}, {**STEPS[1], "n": 1}]),
            dict(steps=[{**STEPS[0], "n": 1.5}]), dict(steps=[{**STEPS[0], "n": True}]), dict(steps=[{**STEPS[0], "n": 0}]),
            dict(steps=[{k: v for k, v in STEPS[0].items() if k != "specialist"}]),
            dict(needs=[{"what": "x", "url": "javascript:alert(1)"}]), dict(needs=[{"what": ""}]), dict(needs="x"),
            dict(needs=[{"what": "x"}] * 21),
        ]
        for over in bad:
            with self.assertRaises(ValueError, msg=str(over)[:60]):
                plan(**over)

    def test_accepts_a_plan_with_no_needs(self):
        self.assertEqual(plan(needs=None)["needs"], [])


TREE = [
    {"id": "s1", "n": 1, "name": "Okta Users", "type": "Control Setup", "specialist": "Rules Specialist", "kind": "control", "scope": "L1", "position": "2.1"},
    {"id": "s2", "n": 2, "name": "Users MFA test", "type": "Rule Setup", "specialist": "Rules Specialist", "kind": "rule", "scope": "L1", "parent": "s1", "makes": "rule", "details": "Rule for control 2.1"},
    {"id": "s3", "n": 3, "name": "MFA Check", "type": "Control Setup", "specialist": "Rules Specialist", "kind": "control", "scope": "L2", "position": "5.6"},
    {"id": "s4", "n": 4, "name": "Feed Okta Users into MFA Check", "type": "Control Dependency", "specialist": "Rules Specialist", "kind": "dependency", "scope": "L1", "ends": {"from_step": "s1", "to_step": "s3"}},
]


class PlanStructure(unittest.TestCase):
    def test_keeps_what_a_step_is_and_what_it_hangs_under(self):
        p = plan(steps=TREE, match="partial", match_note="Needs a new rule", summary="Enforce MFA", assumptions=["Veza is connected"])
        by_id = {s["id"]: s for s in p["steps"]}
        self.assertEqual((by_id["s2"]["kind"], by_id["s2"]["parent"], by_id["s2"]["details"]), ("rule", "s1", "Rule for control 2.1"))
        self.assertEqual(by_id["s4"]["ends"], {"from_step": "s1", "to_step": "s3"})
        self.assertEqual(by_id["s1"]["position"], "2.1")
        self.assertEqual((p["match"], p["match_note"], p["summary"], p["assumptions"]), ("partial", "Needs a new rule", "Enforce MFA", ["Veza is connected"]))

    def test_every_step_must_say_what_it_is_and_which_layer(self):
        for drop in ("kind", "scope"):
            with self.assertRaises(ValueError, msg=drop):
                plan(steps=[{k: v for k, v in TREE[0].items() if k != drop}])
        with self.assertRaises(ValueError):
            plan(steps=[{**TREE[0], "kind": "widget"}])

    def test_refuses_a_plan_that_does_not_hold_together(self):
        bad = [
            [TREE[0], {**TREE[1], "parent": "nope"}],
            [{**TREE[0], "parent": "s1"}],
            [{**TREE[0], "parent": "s2"}, {**TREE[1], "parent": "s1"}],
            [*TREE[:3], {**TREE[3], "ends": {"from_step": "s1", "to_step": "ghost"}}],
            [*TREE[:3], {**TREE[3], "ends": {"from_step": "s1", "to_step": "s1"}}],
            [*TREE[:3], {k: v for k, v in TREE[3].items() if k != "ends"}],
            [{**TREE[0], "ends": {"from_step": "s1", "to_step": "s3"}}, TREE[2]],
            [{**TREE[0], "position": "five"}],
            [{**TREE[0], "position": "5."}],
        ]
        for steps in bad:
            with self.assertRaises(ValueError, msg=str(steps)[:80]):
                plan(steps=steps)

    def test_refuses_a_bad_match_or_assumptions(self):
        for over in (dict(match="mostly"), dict(assumptions="x"), dict(assumptions=[""]), dict(assumptions=["x"] * 31), dict(summary="x" * 1001)):
            with self.assertRaises(ValueError, msg=str(over)):
                plan(**over)


class WhatARuleStepMakes(unittest.TestCase):
    """A rule step says in the plan whether it makes a rule or SQL evidence, from a closed list, so nothing has to read a label."""

    RULE = {"id": "s3", "n": 3, "name": "Check", "type": "Rule Setup", "specialist": "Rules Specialist", "kind": "rule", "scope": "L1", "parent": "s2", "makes": "rule"}

    def made(self, **over):
        return plan(steps=[*STEPS, {**self.RULE, **over}])["steps"][-1]

    def test_the_two_values_are_kept(self):
        for value in c.MAKES:
            self.assertEqual(self.made(makes=value)["makes"], value)
        self.assertEqual(c.MAKES, ("rule", "evidence"))

    def test_a_rule_step_that_says_nothing_is_refused(self):
        steps = [{k: v for k, v in self.RULE.items() if k != "makes"}]
        with self.assertRaises(ValueError) as caught:
            plan(steps=[*STEPS, *steps])
        self.assertIn("needs makes", str(caught.exception))
        self.assertIn("rule, evidence", str(caught.exception))

    def test_a_step_that_is_not_a_rule_needs_nothing(self):
        self.assertNotIn("makes", plan(steps=STEPS)["steps"][0])

    def test_anything_else_is_refused_and_the_error_lists_the_values(self):
        for bad in ("sql", "Rule", "EVIDENCE", "rule ", "", 3, True, ["rule"], None):
            with self.assertRaises(ValueError, msg=repr(bad)) as caught:
                self.made(makes=bad)
            self.assertIn("rule, evidence", str(caught.exception))

    def test_only_a_rule_step_can_say_it(self):
        for kind in ("app", "scope", "control", "link", "dependency", "action", "workflow", "report", "step"):
            extra = {"ends": {"from_step": "s1", "to_step": "s2"}} if kind in c.JOINS else {}
            with self.assertRaises(ValueError, msg=kind) as caught:
                self.made(kind=kind, makes="evidence", **extra)
            self.assertIn("only for a rule step", str(caught.exception))

    def test_it_survives_being_stored_and_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            store = c.RunStore(d)
            plan_id = c.record_plan(store, plan(steps=[*STEPS, {**self.RULE, "makes": "evidence"}]))["plan_id"]
            ledger = c.ledger_of(store.read(plan_id))
            self.assertEqual(ledger["steps"][-1]["spec"]["makes"], "evidence")


class CleanStep(unittest.TestCase):
    def test_reads_a_step_with_what_it_made(self):
        e = c.clean_step("p1", "s1", "done", "Created.", {"type": "application", "name": "Veza", "id": "a-1", "url": "https://x.example/a-1"}, "20261023_27", [{"what": "Add the credential"}])
        self.assertEqual(e["object"], {"type": "application", "name": "Veza", "id": "a-1", "url": "https://x.example/a-1"})
        self.assertEqual((e["status"], e["session"], e["summary"]), ("done", "20261023_27", "Created."))

    def test_accepts_every_status_and_refuses_others(self):
        for s in c.STATUSES:
            self.assertEqual(c.clean_step("p1", "s1", s)["status"], s)
        for s in ("finished", "", None, "DONE"):
            with self.assertRaises(ValueError):
                c.clean_step("p1", "s1", s)

    def test_refuses_a_bad_object_link_or_session(self):
        for over in (dict(object={"type": "x"}), dict(object={"type": "x", "name": "y", "url": "ftp://x"}), dict(session="a b"), dict(summary="x" * 2001), dict(object="x")):
            with self.assertRaises(ValueError, msg=str(over)):
                c.clean_step("p1", "s1", "done", **over)

    def test_accepts_a_model_as_well_as_a_dict(self):
        class Made:
            def model_dump(self):
                return {"type": "control", "name": "MFA"}
        self.assertEqual(c.clean_step("p1", "s1", "done", object=Made())["object"], {"type": "control", "name": "MFA"})


class MintedId(unittest.TestCase):
    def test_keeps_the_name_and_adds_a_suffix_nobody_else_has(self):
        ids = {c.mint_plan_id("okta-mfa-1") for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for i in ids:
            self.assertRegex(i, r"^okta-mfa-1-[0-9a-f]{8}$")
            self.assertEqual(c.clean_step(i, "s1", "done")["plan_id"], i)

    def test_does_not_give_a_name_that_already_has_a_suffix_a_second_one(self):
        first = c.mint_plan_id("okta-mfa-1")
        second = c.mint_plan_id(first)
        self.assertRegex(second, r"^okta-mfa-1-[0-9a-f]{8}$")
        self.assertNotEqual(first, second)

    def test_stays_a_valid_id_whatever_the_name_length(self):
        i = c.mint_plan_id("x" * 80)
        self.assertLessEqual(len(i), 80)
        c.clean_step(i, "s1", "done")


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = c.RunStore(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def put(self, **over):
        """Records a plan and returns the id it was given, which is what every later call has to use."""
        return c.record_plan(self.store, plan(**over))["plan_id"]

    def test_without_a_directory_it_only_validates_and_keeps_nothing(self):
        off = c.RunStore("")
        self.assertEqual(c.record_plan(off, plan())["persisted"], False)
        self.assertEqual(c.record_step(off, c.clean_step("p1", "anything", "done"))["persisted"], False)
        with self.assertRaises(ValueError):
            c.get_run(off, "p1")

    def test_says_which_id_to_use_from_now_on(self):
        out = c.record_plan(self.store, plan())
        self.assertRegex(out["plan_id"], r"^p1-[0-9a-f]{8}$")
        self.assertIn(out["plan_id"], out["next"])

    def test_keeps_a_run_so_it_can_be_picked_up_and_says_what_comes_next(self):
        pid = self.put()
        c.record_step(self.store, c.clean_step(pid, "s1", "started"))
        out = c.record_step(self.store, c.clean_step(pid, "s1", "done"))
        self.assertEqual((out["done"], out["total"], out["next_step_id"]), (1, 2, "s2"))
        run = c.get_run(self.store, pid)
        self.assertEqual([s["status"] for s in run["steps"]], ["done", "pending"])
        self.assertEqual(run["next_step_id"], "s2")

    def test_refuses_a_step_of_an_unknown_plan_or_not_in_the_plan(self):
        with self.assertRaisesRegex(ValueError, "call run_plan first"):
            c.record_step(self.store, c.clean_step("p1", "s1", "done"))
        pid = self.put()
        with self.assertRaisesRegex(ValueError, "not in the plan"):
            c.record_step(self.store, c.clean_step(pid, "s9", "done"))

    def test_the_name_alone_is_not_the_id_of_the_run(self):
        self.put()
        with self.assertRaisesRegex(ValueError, "call run_plan first"):
            c.record_step(self.store, c.clean_step("p1", "s1", "done"))
        with self.assertRaises(ValueError):
            c.get_run(self.store, "p1")

    def test_two_chats_that_name_their_plans_alike_keep_their_runs_apart(self):
        a = self.put()
        b = self.put()
        self.assertNotEqual(a, b)
        c.record_step(self.store, c.clean_step(a, "s1", "done"))
        c.record_step(self.store, c.clean_step(a, "s2", "done"))
        c.record_step(self.store, c.clean_step(b, "s1", "started"))
        self.assertEqual((c.get_run(self.store, a)["done"], c.get_run(self.store, a)["next_step_id"]), (2, None))
        self.assertEqual((c.get_run(self.store, b)["done"], c.get_run(self.store, b)["next_step_id"]), (0, "s1"))
        self.assertEqual(len(os.listdir(self.dir.name)), 2)

    def test_a_changed_plan_is_a_new_run(self):
        first = self.put()
        c.record_step(self.store, c.clean_step(first, "s1", "done"))
        second = self.put(title="Changed")
        self.assertNotEqual(first, second)
        self.assertEqual(c.get_run(self.store, second)["done"], 0)
        self.assertEqual(c.get_run(self.store, first)["done"], 1)

    def test_the_file_is_named_by_the_plan_alone_and_cannot_leave_the_directory(self):
        with self.assertRaises(ValueError):
            c.get_run(self.store, "../escape")
        pid = self.put()
        self.assertEqual(os.listdir(self.dir.name), [f"{pid}.jsonl"])
        with open(os.path.join(self.dir.name, f"{pid}.jsonl")) as f:
            self.assertEqual(json.loads(f.readline())["kind"], "plan")

    def test_skips_a_line_it_cannot_read(self):
        pid = self.put()
        with open(os.path.join(self.dir.name, f"{pid}.jsonl"), "a") as f:
            f.write("not json\n")
        self.assertEqual(c.get_run(self.store, pid)["total"], 2)


if __name__ == "__main__":
    unittest.main()
