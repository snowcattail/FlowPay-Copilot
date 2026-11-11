"""排障模式入口；历史阶段名仍可用于复现实验。"""

from collections.abc import Callable
from enum import StrEnum

from flowpay.core.execution.contracts import TurnInput, TurnResult, Workflow
from flowpay.core.execution.runtime import AgentRuntime
from flowpay.core.execution.stage import StageCard
from flowpay.core.llm.chat import ChatModel
from flowpay.workflows.s1 import S1Workflow
from flowpay.workflows.s2 import S2Workflow
from flowpay.workflows.s3 import S3Workflow
from flowpay.workflows.s4 import S4Workflow, conditions_in

StageFactory = Callable[[ChatModel, Callable[[str], str]], Workflow]

class TroubleshootingMode(StrEnum):
    FAST_FAQ = "FAST_FAQ"
    GUIDED_RETRIEVAL = "GUIDED_RETRIEVAL"
    DEEP_INVESTIGATION = "DEEP_INVESTIGATION"
    INTERACTIVE_DIAGNOSIS = "INTERACTIVE_DIAGNOSIS"


MODE_STAGES: dict[TroubleshootingMode, str] = {
    TroubleshootingMode.FAST_FAQ: "s1",
    TroubleshootingMode.GUIDED_RETRIEVAL: "s2",
    TroubleshootingMode.DEEP_INVESTIGATION: "s3",
    TroubleshootingMode.INTERACTIVE_DIAGNOSIS: "s4",
}

STAGES: dict[str, StageFactory] = {
    "s1": S1Workflow,
    "s2": S2Workflow,
    "s3": S3Workflow,
    "s4": S4Workflow,
}

ROUTE_STATE_KEY = "_troubleshooting_mode"
FAQ_WORDS = ("如何", "怎么", "什么", "是否", "规则", "文档", "含义", "区别", "会怎样")
INCIDENT_WORDS = ("失败", "报错", "异常", "没收到", "查一下", "排查", "为什么", "状态")


def resolve_stage(name: str | TroubleshootingMode | None = None) -> str:
    """把工业模式和旧阶段名归一到已实现阶段；auto 保留首轮分发。"""
    if name is None:
        return "auto"
    if isinstance(name, TroubleshootingMode):
        return MODE_STAGES[name]
    if name in STAGES or name == "auto":
        return name
    try:
        return MODE_STAGES[TroubleshootingMode(name.upper())]
    except (ValueError, KeyError):
        available = ", ".join((*STAGES, "auto", *(mode.value for mode in TroubleshootingMode)))
        raise ValueError(f"Unknown stage: {name}; available: {available}") from None


def route_first_turn(message: str) -> TroubleshootingMode:
    """只有明确的通用规则问答走快速路径；其余交给完整多轮诊断。"""
    text = message.casefold()
    if (
        not conditions_in(message)
        and any(word in text for word in FAQ_WORDS)
        and not any(word in text for word in INCIDENT_WORDS)
    ):
        return TroubleshootingMode.FAST_FAQ
    return TroubleshootingMode.INTERACTIVE_DIAGNOSIS


class RoutedWorkflow:
    """首轮选模式并写入检查点，后续轮次使用同一个工作流。"""

    id: str = "flowpay-troubleshooting"
    version: str = "1"
    card: StageCard = StageCard(
        label="智能排障",
        description="通用规则问题快速回答；账户故障进入多轮诊断",
        examples=S4Workflow.card.examples,
        reply_hint="可连续补充账户、环境及业务编号；诊断条件会跨轮保留。",
    )

    def __init__(self, model: ChatModel, prepare_query: Callable[[str], str]) -> None:
        self.model: ChatModel = model
        self.prepare_query: Callable[[str], str] = prepare_query

    async def run(self, turn: TurnInput, runtime: AgentRuntime) -> TurnResult:
        stored = turn.saved_state.get(ROUTE_STATE_KEY)
        mode = (
            TroubleshootingMode(stored)
            if isinstance(stored, str)
            else route_first_turn(turn.user_message)
        )
        workflow = STAGES[MODE_STAGES[mode]](self.model, self.prepare_query)
        result = await workflow.run(turn, runtime)
        result.saved_state[ROUTE_STATE_KEY] = mode.value
        return result


def create_stage(
    name: str | TroubleshootingMode | None,
    model: ChatModel,
    prepare_query: Callable[[str], str],
) -> Workflow:
    """显式模式直达对应实现，auto 在首轮问题到来时分发。"""
    stage = resolve_stage(name)
    if stage == "auto":
        return RoutedWorkflow(model, prepare_query)
    return STAGES[stage](model, prepare_query)
