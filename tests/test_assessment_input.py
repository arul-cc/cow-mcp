import asyncio
import unittest
from unittest import mock

from tools.rules import action_rules as tool

NOT_FOUND = {"Message": "RESOURCE_NOT_FOUND", "Description": "Resource you requested for is not found"}
PLAN = {"id": "8abc9189-6492-4451-bd48-8714febba589", "name": "ZZ", "inputs": {}}


def run(coro):
    return asyncio.run(coro)


class TheFailureCheck(unittest.TestCase):
    def test_a_message_string_is_a_failure(self):
        self.assertTrue(tool._api_failed("Facing error : Request timed out."))

    def test_each_error_body_is_a_failure(self):
        for body in ({"error": "x"}, {"ErrorMessage": "x"}, {"ErrorDetails": []}, NOT_FOUND):
            self.assertTrue(tool._api_failed(body), body)

    def test_a_plan_is_not_a_failure(self):
        self.assertFalse(tool._api_failed(PLAN))

    def test_a_body_with_only_a_message_is_not_taken_for_a_failure(self):
        self.assertFalse(tool._api_failed({"Message": "saved"}))

    def test_nothing_returned_is_not_a_failure(self):
        self.assertFalse(tool._api_failed({}))
        self.assertFalse(tool._api_failed(None))


class TheTool(unittest.TestCase):
    def add(self, responses, **args):
        call = mock.AsyncMock(side_effect=responses)
        with mock.patch.object(tool.utils, "make_API_call_to_CCow_and_get_response", call):
            result = run(tool.add_assessment_input(**{"id": PLAN["id"], "input_key": "AdminEmail", **args}))
        return result, call

    def test_a_plan_that_does_not_exist_is_an_error_for_a_text_input(self):
        result, _ = self.add([NOT_FOUND], value="a@example.com", input_type="string")
        self.assertFalse(result["success"])
        self.assertIn("RESOURCE_NOT_FOUND", result["error"])

    def test_a_plan_that_does_not_exist_is_an_error_for_a_file_input_and_nothing_else_is_called(self):
        result, call = self.add([NOT_FOUND], value="[]", input_type="file", file_name="a.json")
        self.assertFalse(result["success"])
        self.assertEqual(call.await_count, 1)

    def test_a_plan_that_exists_takes_a_text_input(self):
        result, call = self.add([PLAN], value="a@example.com", input_type="string")
        self.assertEqual(result, {"success": True, "plan_id": PLAN["id"], "input_key": "AdminEmail"})
        self.assertEqual(call.await_count, 1)

    def test_a_plan_that_exists_takes_a_file_input(self):
        update = {"inputs": {"JiraWorkflowConfigFile": {"value": {"FileName": "a.json", "FileHash": "h", "FileContent": None}}}}
        result, call = self.add([PLAN, update, PLAN], input_key="JiraWorkflowConfigFile", value="[]", input_type="file", file_name="a.json")
        self.assertTrue(result["success"])
        self.assertEqual(call.await_count, 3)

    def test_the_update_step_failing_is_reported_for_a_file_input(self):
        result, call = self.add([PLAN, NOT_FOUND], value="[]", input_type="file", file_name="a.json")
        self.assertFalse(result["success"])
        self.assertIn("step 2", result["error"])
        self.assertEqual(call.await_count, 2)


if __name__ == "__main__":
    unittest.main()
