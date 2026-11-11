"""一轮 agent 可以花多少、为什么停下来。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
)

IDENTIFIER = re.compile(r"\b[a-z]{2,8}_[A-Za-z0-9_]*\d[A-Za-z0-9_]*\b")


def estimate_tokens(value: object) -> int:
    """保守估算请求体 token：ASCII 按四字符计，非 ASCII 按一字符计。"""
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    ascii_count = sum(ord(char) < 128 for char in serialized)
    return (ascii_count + 3) // 4 + len(serialized) - ascii_count


def paired_observations(messages: Sequence[ChatCompletionMessageParam]) -> list[int]:
    """验证每组调用都紧邻完整的同 ID 工具结果，并返回结果位置。"""
    observations: list[int] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message["role"] == "tool":
            raise ValueError("Orphan tool result in context")
        calls = message.get("tool_calls") if message["role"] == "assistant" else None
        if not calls:
            index += 1
            continue
        expected = [call["id"] for call in calls]
        if len(expected) != len(set(expected)):
            raise ValueError("Duplicate tool call ID in context")
        following = messages[index + 1 : index + 1 + len(expected)]
        if len(following) != len(expected) or any(
            result["role"] != "tool" for result in following
        ) or sorted(
            cast(ChatCompletionToolMessageParam, result)["tool_call_id"] for result in following
        ) != sorted(expected):
            raise ValueError("Incomplete assistant/tool call pair in context")
        observations.extend(range(index + 1, index + 1 + len(expected)))
        index += 1 + len(expected)
    return observations


def observation_summary(content: str) -> str:
    """保留有限的开头和业务编号，明确告诉模型历史观察已不完整。"""
    identifiers = list(dict.fromkeys(IDENTIFIER.findall(content)))[:16]
    return json.dumps(
        {
            "compacted": True,
            "note": "早期工具结果已折叠，以下仅为摘要；缺失细节需要重新查询。",
            "excerpt": content[:240],
            "identifiers": identifiers,
        },
        ensure_ascii=False,
    )


class StopReason(StrEnum):
    """一轮 agent 结束的原因，写入检查点供评测和界面区分。

    ANSWERED 表示模型自己认为证据够了；BUDGET_EXHAUSTED 表示预算耗尽被要求收尾；
    NEED_USER_INPUT 表示本轮以追问结束，等用户补充条件。NO_EVIDENCE 供后续阶段使用。
    """

    ANSWERED = "answered"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NEED_USER_INPUT = "need_user_input"
    NO_EVIDENCE = "no_evidence"


@dataclass
class Budget:
    """一轮 agent 的模型调用与工具调用额度，以及已经花掉多少。

    阶段在自己的文件里写明额度，循环读三个判断决定下一步：还能不能请求模型
    （may_think）、这次请求要不要带工具（tools_available）、是不是必须收尾
    （must_answer）。额度用完是一次要回传给模型的观察，由工具处理函数说明原因。

    extendable 表示这份额度还可能被 extend 抬高。此时工具额度用尽也不收走工具——
    否则模型就没机会表达"我还需要查"，自适应阶段也就无从判断该不该加码。
    """

    model_calls: int
    tool_calls: int
    spent_model_calls: int = 0
    spent_tool_calls: int = 0
    extendable: bool = False
    context_token_limit: int = 32_000
    context_trigger_ratio: float = 0.85
    context_target_ratio: float = 0.70

    def prepare_context(
        self,
        messages: list[ChatCompletionMessageParam],
        extra: Sequence[ChatCompletionMessageParam] = (),
        tools: Sequence[ChatCompletionToolParam] = (),
    ) -> None:
        """逼近上限时从旧到新折叠观察；不改动调用骨架、系统消息或 Case。"""
        if self.context_token_limit <= 0 or not (
            0 < self.context_target_ratio < self.context_trigger_ratio <= 1
        ):
            raise ValueError("Invalid context token budget")
        observations = paired_observations(messages)

        def size() -> int:
            return estimate_tokens([*messages, *extra]) + estimate_tokens(tools)

        if size() >= self.context_token_limit * self.context_trigger_ratio:
            target = int(self.context_token_limit * self.context_target_ratio)
            for index in observations:
                if size() <= target:
                    break
                message = messages[index]
                content = message.get("content")
                if not isinstance(content, str):
                    continue
                summary = observation_summary(content)
                if estimate_tokens(summary) < estimate_tokens(content):
                    message["content"] = summary
        if size() > self.context_token_limit:
            raise ValueError("Context token budget exceeded after observation compaction")

    def may_think(self) -> bool:
        """是否还能再请求一次模型。"""
        return self.spent_model_calls < self.model_calls

    def has_tool_calls_left(self) -> bool:
        """工具额度是否还有剩余；工具自身用它决定是否执行还是回传一条预算观察。"""
        return self.spent_tool_calls < self.tool_calls

    def must_answer(self) -> bool:
        """本次请求是否必须给出回答：工具额度已尽且不可再抬高，或这是最后一次模型调用。"""
        if self.spent_model_calls >= self.model_calls - 1:
            return True
        return not self.has_tool_calls_left() and not self.extendable

    def tools_available(self) -> bool:
        """本次请求是否还应该把工具交给模型。"""
        return not self.must_answer()

    def extend(self, model_calls: int, tool_calls: int) -> None:
        """抬高本轮额度上限，已花掉的次数保持不变。

        用于执行中升级策略：路由只是初始判断，观察表明投入不够时可以加码。
        只升不降——降额度会让已经发出的调用变成越界。
        """
        if model_calls < self.model_calls or tool_calls < self.tool_calls:
            raise ValueError("Budget can only be extended")
        self.model_calls, self.tool_calls = model_calls, tool_calls

    def spend_model_call(self) -> None:
        """记一次模型调用；超出额度是调用方的循环写错了，直接暴露。"""
        if not self.may_think():
            raise RuntimeError("Model call budget exhausted")
        self.spent_model_calls += 1

    def spend_tool_call(self) -> None:
        """记一次真正执行的工具调用；被拒绝或命中缓存的调用不计入。"""
        self.spent_tool_calls += 1
