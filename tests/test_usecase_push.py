import asyncio
import base64
import json
import os
import unittest
from unittest import mock

import httpx

from tools.playbook import usecase_push as u
from tools.playbook import usecase_tools as tools

VALID = """\
apiVersion: playbook.compliancecow.io/v1
kind: UseCase
metadata:
  id: uc-servicenow-eol
  version: 1.0.0
spec:
  name: Software end of life
  lifecycle: draft
  domainArea: Vulnerability management
  steps:
    - {id: a, type: create_application}
    - {id: b, type: create_application_scope}
    - {id: c, type: create_control}
    - {id: d, type: create_control}
"""


def run(coro):
    return asyncio.run(coro)


class Recorder:
    """A backend that remembers what it was sent and answers as told."""

    def __init__(self, status=200, body=None, problem=None):
        self.sent, self.status, self.body, self.problem = [], status, body, problem

    async def __call__(self, payload):
        self.sent.append(payload)
        return self.status, self.body, self.problem


class Prepare(unittest.TestCase):
    def test_reads_a_use_case_and_names_the_file_after_it(self):
        p = u.prepare(VALID)
        self.assertEqual((p.source_name, p.replace), ("uc-servicenow-eol.yaml", False))
        self.assertEqual(p.data, VALID.encode())

    def test_keeps_the_file_name_it_is_given(self):
        self.assertEqual(u.prepare(VALID, "servicenow.yaml").source_name, "servicenow.yaml")
        self.assertEqual(u.prepare(VALID, "servicenow.YML".lower()).source_name, "servicenow.yml")

    def test_refuses_what_is_not_a_plain_yaml_file_name(self):
        for bad in ("../x.yaml", "a/b.yaml", "x.json", "x", ".yaml", "x y.yaml", "x.yaml\n", "a" * 101 + ".yaml", 5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                u.prepare(VALID, bad)

    def test_refuses_what_is_not_a_use_case(self):
        cases = {
            "nothing": "",
            "spaces": "   \n",
            "not text": 5,
            "unreadable": "a: [1, 2",
            "a list": "- a\n- b\n",
            "a value": "just text",
            "two documents": VALID + "---\n" + VALID,
            "wrong kind": VALID.replace("kind: UseCase", "kind: Rule"),
            "no kind": VALID.replace("kind: UseCase\n", ""),
            "wrong apiVersion": VALID.replace("playbook.compliancecow.io/v1", "other.io/v1"),
            "no apiVersion": VALID.replace("apiVersion: playbook.compliancecow.io/v1\n", ""),
            "no id": VALID.replace("  id: uc-servicenow-eol\n", ""),
            "blank id": VALID.replace("id: uc-servicenow-eol", "id: '  '"),
            "no version": VALID.replace("  version: 1.0.0\n", ""),
            "no spec": "apiVersion: playbook.compliancecow.io/v1\nkind: UseCase\nmetadata: {id: x, version: 1}\n",
            "no steps": VALID.split("  steps:")[0],
            "empty steps": VALID.split("  steps:")[0] + "  steps: []\n",
            "steps not a list": VALID.split("  steps:")[0] + "  steps: a\n",
        }
        for label, text in cases.items():
            with self.assertRaises(ValueError, msg=label):
                u.prepare(text)

    def test_a_number_for_a_version_is_accepted_and_left_to_the_backend(self):
        self.assertEqual(u.prepare(VALID.replace("version: 1.0.0", "version: 2")).doc["metadata"]["version"], 2)

    def test_refuses_a_document_too_large_to_push(self):
        with self.assertRaises(ValueError) as caught:
            u.prepare(VALID + "# " + "x" * u.MAX_BYTES)
        self.assertIn("bytes", str(caught.exception))

    def test_replace_has_to_be_a_real_boolean(self):
        for bad in ("true", 1, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                u.prepare(VALID, None, bad)

    def test_does_not_judge_what_the_backend_judges(self):
        odd = VALID.replace("type: create_application}", "type: nothing_known}")
        self.assertEqual(u.prepare(odd).doc["spec"]["steps"][0]["type"], "nothing_known")


class Preview(unittest.TestCase):
    def test_says_what_would_be_pushed_and_sends_nothing(self):
        out = u.preview(u.prepare(VALID, "servicenow.yaml"))
        self.assertFalse(out["pushed"])
        self.assertTrue(out["needs_approval"])
        self.assertEqual((out["id"], out["version"], out["name"], out["lifecycle"], out["steps"]), ("uc-servicenow-eol", "1.0.0", "Software end of life", "draft", 4))
        self.assertEqual(out["step_types"], {"create_application": 1, "create_application_scope": 1, "create_control": 2})
        self.assertEqual((out["source_name"], out["replace"], out["bytes"]), ("servicenow.yaml", False, len(VALID.encode())))
        self.assertNotIn("warning", out)

    def test_warns_when_it_would_overwrite(self):
        out = u.preview(u.prepare(VALID, None, True))
        self.assertTrue(out["replace"])
        self.assertIn("overwritten", out["warning"])

    def test_the_approval_covers_the_document_the_name_and_replace(self):
        base = u.approval_of(u.prepare(VALID, "a.yaml"))
        self.assertEqual(base, u.approval_of(u.prepare(VALID, "a.yaml")))
        self.assertNotEqual(base, u.approval_of(u.prepare(VALID + "# changed\n", "a.yaml")))
        self.assertNotEqual(base, u.approval_of(u.prepare(VALID, "b.yaml")))
        self.assertNotEqual(base, u.approval_of(u.prepare(VALID, "a.yaml", True)))

    def test_a_step_that_is_not_a_mapping_is_counted_not_crashed_on(self):
        out = u.preview(u.prepare(VALID.replace("    - {id: d, type: create_control}\n", "    - just text\n")))
        self.assertEqual(out["steps"], 4)
        self.assertEqual(out["step_types"]["None"], 1)


class Pushing(unittest.TestCase):
    def go(self, post, approval="right", **over):
        p = u.prepare(VALID, "servicenow.yaml", over.pop("replace", False))
        return run(u.push(p, u.approval_of(p) if approval == "right" else approval, post)), p

    def test_without_the_right_approval_nothing_is_sent(self):
        for approval in (None, "", "right-looking", "x" * 64):
            post = Recorder(body={"status": "success"})
            out, _ = self.go(post, approval)
            self.assertFalse(out["pushed"], repr(approval))
            self.assertIn("approval", out["problem"])
            self.assertEqual(post.sent, [], repr(approval))

    def test_an_approval_for_another_document_does_not_push_this_one(self):
        approved = u.approval_of(u.prepare(VALID + "# something else\n", "servicenow.yaml"))
        post = Recorder(body={"status": "success"})
        out, _ = self.go(post, approved)
        self.assertFalse(out["pushed"])
        self.assertEqual(post.sent, [])

    def test_sends_the_name_and_the_exact_document_encoded(self):
        post = Recorder(body={"status": "success", "loaded": 4})
        out, p = self.go(post)
        self.assertEqual(len(post.sent), 1)
        self.assertEqual(set(post.sent[0]), {"source_name", "doc"}, "replace is left out unless it is asked for")
        self.assertEqual(post.sent[0]["source_name"], "servicenow.yaml")
        self.assertEqual(base64.b64decode(post.sent[0]["doc"]), VALID.encode())
        self.assertTrue(out["pushed"])
        self.assertEqual(out["backend"], {"status": "success", "loaded": 4})

    def test_sends_replace_only_when_asked(self):
        post = Recorder(body={"status": "success"})
        self.go(post, replace=True)
        self.assertIs(post.sent[0]["replace"], True)

    def test_a_document_with_accents_survives_the_encoding(self):
        text = VALID.replace("Software end of life", "Revisión de logiciels périmés – 終了")
        p = u.prepare(text)
        post = Recorder(body={"status": "success"})
        run(u.push(p, u.approval_of(p), post))
        self.assertEqual(base64.b64decode(post.sent[0]["doc"]).decode("utf-8"), text)

    def test_a_validation_failure_comes_back_with_every_error(self):
        errors = ["steps[2].refs.assessment is required", "step 'c' has an unknown type"]
        out, _ = self.go(Recorder(body={"status": "error", "valid": False, "error_type": "ValidationError", "error_count": 2, "errors": errors}))
        self.assertFalse(out["pushed"])
        self.assertEqual(out["validation_errors"], errors)
        self.assertIn("Fix every listed error", out["next"])

    def test_a_conflict_says_so_and_does_not_suggest_overwriting_without_asking(self):
        out, _ = self.go(Recorder(body={"status": "conflict", "error": "uc-servicenow-eol 1.0.0 already exists", "hint": "Pass 'replace': true in payload to overwrite this version."}))
        self.assertFalse(out["pushed"])
        self.assertTrue(out["conflict"])
        self.assertIn("already exists", out["problem"])
        self.assertIn("ask the person", out["hint"])
        self.assertNotIn("Pass 'replace'", out["hint"])

    def test_a_database_error_is_not_a_push(self):
        out, _ = self.go(Recorder(body={"status": "error", "error_type": "DatabaseError", "error": "constraint failed"}))
        self.assertFalse(out["pushed"])
        self.assertEqual(out["problem"], "constraint failed")

    def test_only_the_backends_status_decides_not_the_http_code(self):
        ok_code_bad_status, _ = self.go(Recorder(status=200, body={"status": "error", "error": "no"}))
        self.assertFalse(ok_code_bad_status["pushed"])
        bad_code_good_status, _ = self.go(Recorder(status=500, body={"status": "success"}))
        self.assertTrue(bad_code_good_status["pushed"], "the backend said it, and the code is only the transport")

    def test_an_answer_in_a_form_not_known_is_never_taken_for_a_success(self):
        for body in ({"loaded": 4}, {"data": []}, {"status": "ok"}, {"status": "partial"}, "OK", [], None, 200):
            out, _ = self.go(Recorder(status=200, body=body))
            self.assertFalse(out["pushed"], repr(body))
            self.assertIn("problem", out, repr(body))

    def test_a_backend_that_could_not_be_reached_is_not_a_push(self):
        out, _ = self.go(Recorder(status=None, body=None, problem="the backend could not be reached"))
        self.assertEqual(out, {"pushed": False, "problem": "the backend could not be reached"})

    def test_a_very_long_answer_is_cut_not_dropped(self):
        out, _ = self.go(Recorder(body="x" * 50000))
        self.assertLess(len(out["backend"]), 7000)
        self.assertTrue(out["backend"].endswith("…"))


class TheRealRequest(unittest.TestCase):
    def post(self, handler, payload=None):
        transport = httpx.MockTransport(handler)
        return run(tools._poster(None, transport)(payload or {"source_name": "a.yaml", "doc": "e30="}))

    def test_posts_to_the_load_endpoint_with_the_json_body(self):
        seen = {}

        def handler(request):
            seen.update(url=str(request.url), method=request.method, body=json.loads(request.content))
            return httpx.Response(200, json={"status": "success"})

        status, body, problem = self.post(handler)
        self.assertEqual((status, body, problem), (200, {"status": "success"}, None))
        self.assertTrue(seen["url"].endswith("/v1/graphloader/playbook/load"), seen["url"])
        self.assertEqual((seen["method"], seen["body"]), ("POST", {"source_name": "a.yaml", "doc": "e30="}))

    def test_sends_the_callers_credentials(self):
        seen = {}

        def handler(request):
            seen["headers"] = dict(request.headers)
            return httpx.Response(200, json={"status": "success"})

        with mock.patch.object(tools.utils, "get_cc_headers", return_value={"Authorization": "caller-token", "X-Cow-Security-Context": "{}"}):
            self.post(handler)
        self.assertEqual(seen["headers"]["authorization"], "caller-token")
        self.assertEqual(seen["headers"]["x-cow-security-context"], "{}")

    def test_keeps_the_whole_body_of_an_error(self):
        body = {"status": "error", "errors": ["a", "b"], "error_type": "ValidationError"}
        status, got, problem = self.post(lambda r: httpx.Response(400, json=body))
        self.assertEqual((status, got, problem), (400, body, None))

    def test_a_body_that_is_not_json_is_kept_as_text(self):
        status, got, problem = self.post(lambda r: httpx.Response(502, text="Bad gateway"))
        self.assertEqual((status, got, problem), (502, "Bad gateway", None))

    def test_a_timeout_says_it_is_not_known_whether_it_was_pushed(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        status, body, problem = self.post(handler)
        self.assertIsNone(status)
        self.assertIn("not known whether", problem)

    def test_a_connection_failure_says_nothing_was_pushed(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        status, body, problem = self.post(handler)
        self.assertIsNone(status)
        self.assertIn("nothing was pushed", problem)
        self.assertNotIn("refused", problem, "no internal detail from the exception")


class Switch(unittest.TestCase):
    def test_it_is_off_unless_asked_for(self):
        for value, want in (("", False), ("0", False), ("no", False), ("1", True), ("true", True), ("YES", True), (" on ", True)):
            with mock.patch.dict(os.environ, {"PLAYBOOK_PUSH_ENABLED": value}):
                self.assertIs(tools.enabled(), want, repr(value))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(tools.enabled())


if __name__ == "__main__":
    unittest.main()
