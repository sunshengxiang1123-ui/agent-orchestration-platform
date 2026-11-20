"""测试共用的计划样例与模型服务替身（httpx.MockTransport，不联网、不计费）。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

GOOD_PLAN: dict[str, Any] = {
    "stops": [
        {"spot_id": "park", "start_minute": 540},
        {"spot_id": "museum", "start_minute": 650},
        {"spot_id": "garden", "start_minute": 790},
    ],
    "rationale": "省钱组合：公园→博物馆→植物园",
}
# 观景塔 10:00 才开门，且总价 270 元超过默认预算 200 元。
BAD_PLAN: dict[str, Any] = {
    "stops": [
        {"spot_id": "tower", "start_minute": 540},
        {"spot_id": "museum", "start_minute": 620},
        {"spot_id": "gallery", "start_minute": 760},
    ],
    "rationale": "故意违规的计划",
}
TEST_KEY = "sk-test-1234567890abcd"
# 替身模型第一轮会调用的工具（只调用实际提供给它的那些）。
FIRST_TURN_TOOLS = ("catalog", "constraints", "knowledge_search")


def is_reviewer(body: dict[str, Any]) -> bool:
    return "审核当前计划" in body["messages"][0]["content"]


def offered_tools(body: dict[str, Any]) -> list[str]:
    return [item["function"]["name"] for item in body.get("tools") or []]


class FakeModel:
    """模拟支持 function calling 的模型：第一轮按需调用工具，拿到结果后给出最终 JSON。"""

    def __init__(
        self,
        plans: list[dict[str, Any]],
        *,
        approve: bool = True,
        status_code: int = 200,
        tools_supported: bool = True,
        use_tools: bool = True,
        greedy_tools: bool = False,
        block_reviewer: bool = False,
    ) -> None:
        self.plans, self.approve, self.status_code = plans, approve, status_code
        self.tools_supported, self.use_tools = tools_supported, use_tools
        # greedy：只要允许就一直调用工具，用来验证最后一轮强制作答。
        self.greedy_tools = greedy_tools
        # block_reviewer：审核请求一直挂起，用来模拟服务在运行中途被停止。
        self.block_reviewer = block_reviewer
        self.reviewer_started = asyncio.Event()
        self.counts = {"planner": 0, "reviewer": 0, "tool_turns": 0, "models": 0}
        self.requests: list[dict[str, Any]] = []

    def final_requests(self, role: str) -> list[dict[str, Any]]:
        """拿到最终答案的那些请求（规划或审核）。"""
        return [
            body
            for body in self.requests
            if is_reviewer(body) == (role == "reviewer")
            and (
                not body.get("tools")
                or body.get("tool_choice") == "none"
                or any(m["role"] == "tool" for m in body["messages"])
            )
        ]

    def transport(self) -> Any:
        import httpx

        def reply(body: dict[str, Any], message: dict[str, Any]) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "choices": [{"message": message}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
            )

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                self.counts["models"] += 1
                return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})
            if self.status_code != 200:
                return httpx.Response(self.status_code, json={"error": "denied"})
            body = json.loads(request.content)
            self.requests.append(body)
            if self.block_reviewer and is_reviewer(body):
                self.reviewer_started.set()
                await asyncio.Event().wait()
            if body.get("tools") and not self.tools_supported:
                return httpx.Response(400, json={"error": "tools not supported"})
            answered = any(message["role"] == "tool" for message in body["messages"])
            wants_tools = self.greedy_tools or not answered
            if (
                self.use_tools
                and body.get("tools")
                and body.get("tool_choice") != "none"
                and wants_tools
            ):
                names = [name for name in FIRST_TURN_TOOLS if name in offered_tools(body)]
                self.counts["tool_turns"] += 1
                calls = [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(
                                {"query": "预算 交通 参考行程"}
                                if name == "knowledge_search"
                                else {}
                            ),
                        },
                    }
                    for index, name in enumerate(names)
                ]
                return reply(body, {"content": "", "tool_calls": calls})
            if is_reviewer(body):
                self.counts["reviewer"] += 1
                content: dict[str, Any] = {
                    "approved": self.approve,
                    "feedback": [] if self.approve else ["行程理由不充分"],
                }
            else:
                index = min(self.counts["planner"], len(self.plans) - 1)
                self.counts["planner"] += 1
                content = self.plans[index]
            return reply(body, {"content": json.dumps(content, ensure_ascii=False)})

        return httpx.MockTransport(handle)
