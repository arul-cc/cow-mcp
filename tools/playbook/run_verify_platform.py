"""The Reader the checks in run_verify.py use against the real platform, with the caller's own credentials."""
from __future__ import annotations

from fastmcp import Context

from constants import constants
from tools.playbook.run_verify import Lookup, answer_of, first_item_of, items_of
from utils import assistant, utils


class PlatformReader:
    def __init__(self, ctx: Context | None):
        self.ctx = ctx

    async def control(self, control_id: str) -> Lookup:
        response = await utils.make_GET_API_call_to_CCow(f"{constants.URL_PLAN_CONTROLS}/{control_id}", ctx=self.ctx)
        return answer_of(response, f"control {control_id}")

    async def scope(self, scope_id: str) -> Lookup:
        response = await utils.make_GET_API_call_to_CCow(constants.URL_APPLICATION_SCOPE, ctx=self.ctx, query_params={"id": scope_id})
        return first_item_of(response, f"application scope {scope_id}")

    async def application(self, application_id: str) -> Lookup:
        response = await utils.make_GET_API_call_to_CCow(constants.URL_CREATE_APPLICATION, ctx=self.ctx, query_params={"id": application_id})
        return first_item_of(response, f"application {application_id}")

    async def links(self, source_plan_id: str, source_control_id: str, target_plan_id: str, target_control_id: str) -> Lookup:
        response = await assistant.fetch_control_links_api(
            source_plan_id=source_plan_id, source_plan_control_id=source_control_id,
            target_plan_id=target_plan_id, target_plan_control_id=target_control_id, ctx=self.ctx,
        )
        return items_of(response, "the link")
