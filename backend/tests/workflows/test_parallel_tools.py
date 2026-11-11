"""同一模型响应中的工具可并发执行，观察仍按调用顺序回填。"""

import asyncio
from typing import cast

import pytest

from flowpay.core.agent.trace import Tracer
from flowpay.core.data_sources import SourceClient
from flowpay.core.execution.contracts import TurnInput
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.json import JsonObject, require_list, require_object
from flowpay.core.llm.embedding.ollama import retrieval_input
from flowpay.router import create_stage

from .fixtures import Model, Search, completion, grounded, search_call


class RecordingTracer:
    """在内存中按 emit 调用顺序编号，避免测试依赖数据库调度。"""

    def __init__(self) -> None:
        self.events: list[JsonObject] = []

    async def emit(
        self,
        kind: str,
        payload: JsonObject,
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> JsonObject:
        event: JsonObject = {
            "type": kind,
            "payload": payload,
            "call_id": call_id,
            "parent_call_id": parent_call_id,
            "seq": len(self.events) + 1,
        }
        self.events.append(event)
        return event


class ConcurrentSearch(Search):
    """首个查询等待第二个进入，第二个先完成以检验回填顺序。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = 0
        self.both_entered = asyncio.Event()

    async def search(
        self,
        collection_id: str,
        text: str,
        limit: int = 5,
        filters: JsonObject | None = None,
    ):
        self.entered += 1
        if self.entered == 2:
            self.both_entered.set()
        await self.both_entered.wait()
        if "first" in text:
            await asyncio.sleep(0.02)
        return await super().search(collection_id, text, limit, filters)


class EmptySources:
    """文档工具测试只需业务工具声明时使用的字段枚举。"""

    def field_values(self, kind: str, source: str) -> dict[str, list[str]]:
        _ = kind, source
        return {}


@pytest.mark.parametrize("stage", ["s2", "s3", "s4"])
async def test_independent_tool_calls_run_concurrently_and_observe_in_model_order(
    stage: str,
) -> None:
    model, search = Model(), ConcurrentSearch()
    recorder = RecordingTracer()
    tracer = cast(Tracer, recorder)
    runtime = AgentRuntime(
        tracer, sources=cast(SourceClient, EmptySources()),
        knowledge=search.for_run(tracer), collection_id="fixture",
    )
    model.responses = [
        completion(calls=[search_call("first", "first"), search_call("second", "second")]),
        grounded(),
    ]

    workflow = create_stage(stage, model, retrieval_input)
    outcome = await asyncio.wait_for(
        workflow.run(TurnInput("端点规则", [], {}), runtime),
        timeout=3,
    )

    assert outcome.text
    assert search.entered == 2
    messages = require_list(model.requests[1]["messages"])
    tool_messages = [
        require_object(item) for item in messages if require_object(item)["role"] == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == ["first", "second"]
    events = recorder.events
    started = [event for event in events if event["type"] == "tool.started"]
    assert [event["call_id"] for event in started] == ["first", "second"]
    assert [event["seq"] for event in started] == sorted(event["seq"] for event in started)
