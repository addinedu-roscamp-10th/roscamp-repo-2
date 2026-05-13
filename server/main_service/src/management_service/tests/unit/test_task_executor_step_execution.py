from __future__ import annotations

import asyncio

import pytest

from management_service.tests.unit.test_task_executor import (
    _RecordingAdapter,
    _RecordingStateManager,
    _event_bridge,
    _execute_input,
)
from services.contracts.enums import TaskType, TxnStat
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
        state_manager = _RecordingStateManager(charger_slot=(1, 2))
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
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(charger_slot=(9, 9)), None)
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


def test_execute_task_runs_tochg_sequence_with_injected_charge_pose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ToCHG 전체 실행은 충전 위치를 주입한 뒤 adapter 호출과 성공 상태 전이까지 마친다."""

    async def fake_sleep(_: float) -> None:
        return None

    async def scenario() -> None:
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        adapter = _RecordingAdapter()
        state_manager = _RecordingStateManager(charger_slot=(1, 2))
        executor = TaskExecutor(adapter, state_manager, None)

        result = await executor.execute_task(_execute_input(TaskType.ToCHG))

        assert result.final_status == TxnStat.SUCC
        assert result.steps_executed == 1
        assert state_manager.status_updates == [
            {"task_id": "task_1", "new_stat": "PROC", "error_code": None},
            {"task_id": "task_1", "new_stat": "SUCC", "error_code": None},
        ]
        assert state_manager.charger_requests == ["TAT2"]
        assert adapter.calls == [
            {
                "res_id": "TAT2",
                "action": "dock_robot",
                "params": {"pose_name": "ToCHG2"},
            }
        ]

    asyncio.run(scenario())


def test_execute_task_runs_topp_sequence_without_inline_charge_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ToPP 전체 실행은 업무 이동까지만 수행하고 충전 복귀는 포함하지 않는다."""

    async def fake_sleep(_: float) -> None:
        return None

    async def fake_wait_task_completed(*_args, **_kwargs) -> bool:
        return True

    async def fake_wait_subtask_completed(*_args, **_kwargs) -> bool:
        return True

    async def scenario() -> None:
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        adapter = _RecordingAdapter()
        state_manager = _RecordingStateManager(charger_slot=(1, 3))
        executor = TaskExecutor(adapter, state_manager, _event_bridge())
        monkeypatch.setattr(executor, "_wait_for_task_completed", fake_wait_task_completed)
        monkeypatch.setattr(executor, "_wait_for_subtask_completed", fake_wait_subtask_completed)

        result = await executor.execute_task(_execute_input(TaskType.ToPP))

        assert result.final_status == TxnStat.SUCC
        assert result.steps_executed == 3
        assert state_manager.charger_requests == []
        assert adapter.calls == [
            {
                "res_id": "TAT2",
                "action": "dock_robot",
                "params": {"pose_name": "ToCAST"},
            },
            {
                "res_id": "TAT2",
                "action": "dock_robot",
                "params": {"pose_name": "ToPP"},
            },
        ]
        assert state_manager.status_updates == [
            {"task_id": "task_1", "new_stat": "PROC", "error_code": None},
            {"task_id": "task_1", "new_stat": "SUCC", "error_code": None},
        ]

    asyncio.run(scenario())
