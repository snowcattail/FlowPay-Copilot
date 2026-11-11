"""当前阶段入口；不启动服务或调用真实模型。"""

from dataclasses import replace
from pathlib import Path

import pytest

from flowpay.bootstrap import create_workflow
from flowpay.cli import Arguments, configured_runtime, main
from flowpay.core.config import load_settings
from flowpay.router import (
    ROUTE_STATE_KEY,
    RoutedWorkflow,
    TroubleshootingMode,
    resolve_stage,
    route_first_turn,
)
from flowpay.workflows.s1 import S1Workflow
from flowpay.workflows.s2 import S2Workflow
from flowpay.workflows.s3 import S3Workflow

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "backend/configs/local.toml"


def test_stage_overrides_environment_and_keeps_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式阶段优先于环境，保留模型和向量配置。"""
    monkeypatch.setenv("FLOWPAY_WORKFLOW", "s4")
    settings = configured_runtime(Arguments(config=CONFIG, stage="s1"))
    original = load_settings(CONFIG)
    assert settings.workflow == "s1"
    assert settings.embedding == original.embedding
    assert settings.model == original.model
    assert settings.knowledge_collection_id == original.knowledge_collection_id
    for key in (settings.model.api_key_env, settings.knowledge_url_env):
        monkeypatch.setenv(key, "fixture")
    assert settings.embedding is not None
    monkeypatch.setenv(settings.embedding.api_key_env, "fixture")
    workflow = create_workflow(settings)
    assert isinstance(workflow, S1Workflow)
    assert (workflow.id, workflow.version) == ("flowpay-document-rag", "4")


@pytest.mark.parametrize("selection", ["s1", "s2", "s3", "s4"])
def test_environment_stage_selection(monkeypatch: pytest.MonkeyPatch, selection: str) -> None:
    """环境变量使用当前注册阶段名。"""
    monkeypatch.setenv("FLOWPAY_WORKFLOW", selection)
    assert configured_runtime(Arguments(config=CONFIG)).workflow == selection


def test_default_and_named_modes() -> None:
    assert resolve_stage() == "auto"
    assert resolve_stage(TroubleshootingMode.FAST_FAQ) == "s1"
    assert resolve_stage("DEEP_INVESTIGATION") == "s3"
    assert resolve_stage("INTERACTIVE_DIAGNOSIS") == "s4"
    assert route_first_turn("Webhook 返回 410 后会怎样？") is TroubleshootingMode.FAST_FAQ
    assert (
        route_first_turn("生产环境 acct_bg_001 的支付失败，为什么？")
        is TroubleshootingMode.INTERACTIVE_DIAGNOSIS
    )
    assert route_first_turn("支付没收到通知") is TroubleshootingMode.INTERACTIVE_DIAGNOSIS


def test_auto_route_is_fixed_after_first_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    import flowpay.router as router
    from flowpay.core.execution.contracts import TurnInput, TurnResult

    chosen: list[str] = []

    class FakeWorkflow:
        def __init__(self, stage: str) -> None:
            self.stage = stage

        async def run(self, turn: TurnInput, runtime: object) -> TurnResult:
            chosen.append(self.stage)
            return TurnResult("ok", dict(turn.saved_state))

    monkeypatch.setitem(router.STAGES, "s1", lambda model, prepare: FakeWorkflow("s1"))
    monkeypatch.setitem(router.STAGES, "s4", lambda model, prepare: FakeWorkflow("s4"))
    workflow = RoutedWorkflow(None, lambda query: query)
    with pytest.raises(StopIteration) as first_return:
        workflow.run(TurnInput("什么是幂等键？", [], {}), None).send(None)
    first = first_return.value.value
    assert first.saved_state[ROUTE_STATE_KEY] == "FAST_FAQ"
    with pytest.raises(StopIteration) as second_return:
        workflow.run(
            TurnInput("acct_bg_001 生产环境支付失败", [], first.saved_state), None
        ).send(None)
    second = second_return.value.value
    assert second.saved_state[ROUTE_STATE_KEY] == "FAST_FAQ"
    assert chosen == ["s1", "s1"]


@pytest.mark.parametrize("stage", ["s3", "s4"])
def test_business_stages_require_an_access_policy_before_starting(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """用业务数据源的阶段缺少访问策略时在启动前失败，不跑到一半才发现。"""
    settings = configured_runtime(Arguments(config=CONFIG, stage=stage))
    assert settings.embedding is not None
    for key in (
        settings.model.api_key_env,
        settings.embedding.api_key_env,
        settings.knowledge_url_env,
    ):
        monkeypatch.setenv(key, "fixture")
    with pytest.raises(ValueError, match="sources_config"):
        create_workflow(replace(settings, sources_config=None))


@pytest.mark.parametrize(
    "stage, message",
    [
        ("unknown", "Unknown stage"),
        ("demo", "Unknown stage"),
        ("live", "Unknown stage"),
        ("rag", "Unknown stage"),
    ],
)
def test_invalid_or_retired_entry_is_explicit(stage: str, message: str) -> None:
    """拼写错误和旧演示模式提供明确错误，不伪装成课程运行。"""
    with pytest.raises(ValueError, match=message):
        resolve_stage(stage)


def test_removed_workflow_argument_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """命令行只提供 --stage，旧参数在装配外部依赖之前拒绝。"""
    monkeypatch.setattr("sys.argv", ["flowpay", "--workflow", "rag"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


@pytest.mark.parametrize("stage, expected", [("s2", S2Workflow), ("s3", S3Workflow)])
def test_stage_is_assembled_with_shared_connections(
    monkeypatch: pytest.MonkeyPatch, stage: str, expected: type[S2Workflow] | type[S3Workflow]
) -> None:
    """启动阶段选择 s2 或 s3，复用现有模型、向量与知识配置。"""
    settings = configured_runtime(Arguments(config=CONFIG, stage=stage))
    assert settings.embedding is not None
    for key in (
        settings.model.api_key_env,
        settings.embedding.api_key_env,
        settings.knowledge_url_env,
    ):
        monkeypatch.setenv(key, "fixture")
    assert isinstance(create_workflow(settings), expected)
