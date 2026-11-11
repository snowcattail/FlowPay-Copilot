"""长历史压缩必须保持工具调用协议与当前 Case 条件。"""

import json
from typing import cast

import pytest
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionToolMessageParam,
)

from flowpay.core.agent.budget import Budget, estimate_tokens, paired_observations


def call(identifier: str) -> ChatCompletionAssistantMessageParam:
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {"name": "search_documents", "arguments": "{}"},
            }
        ],
    }


def result(identifier: str, size: int = 2000) -> ChatCompletionMessageParam:
    return {"role": "tool", "tool_call_id": identifier, "content": "x" * size}


def test_long_history_compacts_old_observations_and_preserves_pairs() -> None:
    messages: list[ChatCompletionMessageParam] = [{"role": "system", "content": "规则"}]
    for number in range(6):
        identifier = f"call-{number}"
        messages.extend(
            [{"role": "user", "content": f"第 {number} 轮"}, call(identifier), result(identifier)]
        )
    case = "已知条件：account_id=acct_bg_001；environment=production；payment_id=pay_bg_001_0003"
    messages.extend([{"role": "user", "content": "继续排查"}, {"role": "system", "content": case}])
    before = estimate_tokens(messages)
    budget = Budget(10, 8, context_token_limit=2600)
    budget.prepare_context(messages)
    assert estimate_tokens(messages) < before
    assert estimate_tokens(messages) <= budget.context_token_limit
    assert len(paired_observations(messages)) == 6
    assert messages[-1].get("content") == case
    summaries = [
        cast(str, message["content"])
        for message in messages
        if message["role"] == "tool"
    ]
    assert any(json.loads(content)["compacted"] for content in summaries if content.startswith("{"))


def test_parallel_calls_remain_complete_and_ids_unchanged() -> None:
    assistant: ChatCompletionAssistantMessageParam = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {"name": "search", "arguments": "{}"},
            }
            for identifier in ("one", "two")
        ],
    }
    messages: list[ChatCompletionMessageParam] = [assistant, result("two"), result("one")]
    Budget(2, 2, context_token_limit=700).prepare_context(messages)
    assert [
        cast(ChatCompletionToolMessageParam, messages[i])["tool_call_id"]
        for i in paired_observations(messages)
    ] == ["two", "one"]


def test_parallel_group_rejects_duplicate_result_ids() -> None:
    messages: list[ChatCompletionMessageParam] = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": identifier,
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
                for identifier in ("one", "two")
            ],
        },
        result("one"),
        result("one"),
    ]
    with pytest.raises(ValueError, match="pair"):
        Budget(2, 2).prepare_context(messages)


@pytest.mark.parametrize(
    "messages",
    [[call("one")], [result("one")], [call("one"), result("wrong")]],
)
def test_incomplete_or_mismatched_pairs_rejected(
    messages: list[ChatCompletionMessageParam],
) -> None:
    with pytest.raises(ValueError, match="pair|Orphan"):
        Budget(2, 2).prepare_context(messages)


def test_irreducible_context_fails_before_model_call() -> None:
    messages: list[ChatCompletionMessageParam] = [{"role": "system", "content": "政策" * 2000}]
    with pytest.raises(ValueError, match="Context token budget"):
        Budget(2, 2, context_token_limit=300).prepare_context(messages)
