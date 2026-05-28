from __future__ import annotations

import asyncio

import pytest

from management_service.tests.unit.test_task_executor import (
    _RecordingAdapter,
    _RecordingStateManager,
    _execute_input,
)
from services.contracts.enums import TaskType
from services.contracts.models import AdapterResult, CommandStep
from services.core.task_executor import TaskExecutor


@pytest.mark.parametrize(
    ("task_type", "step", "expected_subtask"),
    [
        (
            TaskType.POUR,
            CommandStep(step_id=1, action="metal_pour_action", params={}),
            "pour",
        ),
        (
            TaskType.ToPP,
            CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToCAST"}),
            "topp",
        ),
        (
            TaskType.ToSTRG,
            CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToINSP"}),
            "tostrg",
        ),
        (
            TaskType.ToSTRG,
            CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToSTRG"}),
            "tostrg_dld",
        ),
        (
            TaskType.PA_GP,
            CommandStep(step_id=1, action="pat_pick_action", params={}),
            "pa_dld_done",
        ),
        (
            TaskType.PA_DP,
            CommandStep(step_id=1, action="pat_pick_action", params={}),
            "pa_dld_done",
        ),
    ],
)
def test_handle_step_completion_publishes_expected_subtasks(
    task_type: TaskType,
    step: CommandStep,
    expected_subtask: str,
) -> None:
    """단계 완료 후 공정별로 기대한 세부 작업 완료 이벤트를 발행한다."""

    async def scenario() -> None:
        state_manager = _RecordingStateManager()
        executor = TaskExecutor(_RecordingAdapter(), state_manager, None)

        await executor._handle_step_completion(
            _execute_input(task_type),
            step,
            AdapterResult(success=True, message="ok"),
        )

        assert state_manager.subtask_publications == [
            {
                "task_id": "task_1",
                "item_id": 1001,
                "subtask_type": expected_subtask,
                "task_type": task_type,
            }
        ]

    asyncio.run(scenario())


def test_handle_step_completion_does_not_publish_for_unrelated_steps() -> None:
    """정의된 공정 전환 지점이 아니면 세부 작업 완료 이벤트를 발행하지 않는다."""

    async def scenario() -> None:
        state_manager = _RecordingStateManager()
        executor = TaskExecutor(_RecordingAdapter(), state_manager, None)

        await executor._handle_step_completion(
            _execute_input(TaskType.MM),
            CommandStep(step_id=1, action="pattern_pick_action", params={}),
            AdapterResult(success=True, message="ok"),
        )

        assert state_manager.subtask_publications == []

    asyncio.run(scenario())


def test_handle_step_completion_converts_string_item_id_to_integer() -> None:
    """입력의 item_id가 문자열이어도 발행 시에는 정수로 기록한다."""

    async def scenario() -> None:
        state_manager = _RecordingStateManager()
        executor = TaskExecutor(_RecordingAdapter(), state_manager, None)

        await executor._handle_step_completion(
            _execute_input(TaskType.POUR, item_id="4321"),
            CommandStep(step_id=1, action="metal_pour_action", params={}),
            AdapterResult(success=True, message="ok"),
        )

        assert state_manager.subtask_publications[0]["item_id"] == 4321

    asyncio.run(scenario())
