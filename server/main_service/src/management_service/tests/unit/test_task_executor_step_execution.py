from __future__ import annotations

import asyncio

import pytest

from management_service.tests.unit.test_task_executor import (
    _RecordingAdapter,
    _RecordingStateManager,
    _event_bridge,
    _execute_input,
)
from services.contracts.enums import TaskType
from services.contracts.models import AdapterResult, CommandStep
from services.core.task_executor import TaskExecutor


def test_execute_step_returns_noop_result_for_noop_action() -> None:
    """NOOP step은 추가 동작 없이 성공 결과를 반환한다."""

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), None)
        result = await executor._execute_step(
            _execute_input(TaskType.PP),
            CommandStep(step_id=1, action="NOOP", params={}),
        )

        assert result.success is True
        assert result.message == "noop"

    asyncio.run(scenario())


def test_execute_step_wait_time_calls_asyncio_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """WAIT_TIME step은 지정한 시간만큼 sleep을 호출한다."""

    recorded: list[float] = []

    async def fake_sleep(duration: float) -> None:
        recorded.append(duration)

    async def scenario() -> None:
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), None)
        result = await executor._execute_step(
            _execute_input(TaskType.ToSHIP),
            CommandStep(step_id=1, action="WAIT_TIME", params={"duration_sec": 5}),
        )

        assert result.success is True
        assert result.message == "wait_time"

    asyncio.run(scenario())
    assert recorded == [5]


def test_execute_step_delegates_wait_task_completed_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """WAIT_TASK_COMPLETED step은 내부 대기 함수에 위임한다."""

    recorded: list[tuple[str, dict[str, object]]] = []

    async def fake_wait(input_data, step) -> bool:
        recorded.append((input_data.task_id, step.params))
        return True

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), _event_bridge())
        monkeypatch.setattr(executor, "_wait_for_task_completed", fake_wait)
        result = await executor._execute_step(
            _execute_input(TaskType.ToPP),
            CommandStep(step_id=1, action="WAIT_TASK_COMPLETED", params={"task_type": TaskType.DM}),
        )

        assert result.success is True
        assert result.message == "wait_task_completed"

    asyncio.run(scenario())
    assert recorded == [("task_1", {"task_type": TaskType.DM})]


def test_execute_step_delegates_wait_subtask_completed_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """WAIT_SUBTASK_COMPLETED step은 내부 대기 함수에 위임한다."""

    recorded: list[tuple[str, dict[str, object]]] = []

    async def fake_wait(input_data, step) -> bool:
        recorded.append((input_data.task_id, step.params))
        return True

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), _event_bridge())
        monkeypatch.setattr(executor, "_wait_for_subtask_completed", fake_wait)
        result = await executor._execute_step(
            _execute_input(TaskType.ToSTRG),
            CommandStep(step_id=1, action="WAIT_SUBTASK_COMPLETED", params={"subtask_type": "pa_dld_done"}),
        )

        assert result.success is True
        assert result.message == "wait_subtask_completed"

    asyncio.run(scenario())
    assert recorded == [("task_1", {"subtask_type": "pa_dld_done"})]


def test_execute_step_preserves_existing_pose_name_for_dock_robot() -> None:
    """dock_robot step에 목적지가 이미 있으면 충전 위치를 다시 계산하지 않는다."""

    async def scenario() -> None:
        adapter = _RecordingAdapter()
        state_manager = _RecordingStateManager()
        executor = TaskExecutor(adapter, state_manager, None)
        step = CommandStep(step_id=1, action="dock_robot", params={"pose_name": "ToCAST"})

        result = await executor._execute_step(_execute_input(TaskType.ToPP), step)

        assert result.success is True
        assert state_manager.charger_requests == []
        assert adapter.calls == [
            {
                "res_id": "TAT2",
                "action": "dock_robot",
                "params": {"pose_name": "ToCAST"},
            }
        ]

    asyncio.run(scenario())


def test_execute_step_injects_charger_pose_for_charge_return_steps_without_pose_name() -> None:
    """충전 복귀 단계에 목적지가 비어 있으면 예약된 충전 위치를 계산해 주입한다."""

    async def scenario() -> None:
        adapter = _RecordingAdapter()
        state_manager = _RecordingStateManager(charger_slot="1-2")
        executor = TaskExecutor(adapter, state_manager, None)
        step = CommandStep(step_id=1, action="dock_robot", params={})

        result = await executor._execute_step(_execute_input(TaskType.ToCHG), step)

        assert result.success is True
        assert state_manager.charger_requests == ["TAT2"]
        assert adapter.calls == [
            {
                "res_id": "TAT2",
                "action": "dock_robot",
                "params": {"pose_name": "ToCHG2"},
            }
        ]

    asyncio.run(scenario())


def test_execute_step_fails_when_no_available_charger_is_returned() -> None:
    """충전 위치를 구하지 못하면 dock_robot step을 실패 처리한다."""

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(charger_slot=None), None)
        step = CommandStep(step_id=1, action="dock_robot", params={})

        result = await executor._execute_step(_execute_input(TaskType.ToCHG), step)

        assert result.success is False
        assert result.message == "no_available_charger"

    asyncio.run(scenario())


def test_execute_step_fails_when_charger_pose_mapping_is_missing() -> None:
    """충전 위치는 받았지만 도킹 목적지로 바꾸지 못하면 실패 처리한다."""

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(charger_slot="9-9"), None)
        step = CommandStep(step_id=1, action="dock_robot", params={})

        result = await executor._execute_step(_execute_input(TaskType.ToCHG), step)

        assert result.success is False
        assert result.message == "no_available_charger"

    asyncio.run(scenario())


def test_execute_step_passes_through_non_dock_adapter_calls() -> None:
    """일반 action은 입력 payload를 합쳐 그대로 adapter에 전달한다."""

    async def scenario() -> None:
        adapter = _RecordingAdapter()
        executor = TaskExecutor(adapter, _RecordingStateManager(), None)
        step = CommandStep(step_id=1, action="AI_INFERENCE_REQUEST", params={"model": "vision"})

        result = await executor._execute_step(
            _execute_input(TaskType.INSP, payload={"item_id": 1001}),
            step,
        )

        assert result.success is True
        assert adapter.calls == [
            {
                "res_id": "TAT2",
                "action": "AI_INFERENCE_REQUEST",
                "params": {"model": "vision", "item_id": 1001},
            }
        ]

    asyncio.run(scenario())
