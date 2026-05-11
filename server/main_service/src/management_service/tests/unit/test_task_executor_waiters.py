from __future__ import annotations

import asyncio

import pytest

from management_service.tests.unit.test_task_executor import (
    _RecordingAdapter,
    _RecordingStateManager,
    _event_bridge,
    _execute_input,
)
from services.contracts.enums import EventType, TaskType, TxnStat
from services.contracts.models import CommandStep, Event
from services.core.task_executor import TaskExecutor


def test_wait_task_completed_stays_blocked_until_matching_success_event_arrives() -> None:
    """선행 태스크 완료 이벤트가 오기 전까지 대기하다가, 일치하는 성공 이벤트가 오면 이어서 실행한다."""

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), _event_bridge())
        step = CommandStep(
            step_id=1,
            action="WAIT_TASK_COMPLETED",
            params={"task_type": TaskType.DM},
            timeout_sec=1,
        )

        waiter = asyncio.create_task(executor._execute_step(_execute_input(TaskType.ToPP), step))
        await asyncio.sleep(0)
        assert not waiter.done()

        executor.event_bridge.publish(
            Event(
                event_type=EventType.TASK_COMPLETED,
                item_id=1001,
                payload={"task_type": TaskType.DM, "status": TxnStat.SUCC.value},
            )
        )

        result = await waiter
        assert result.success is True
        assert result.message == "wait_task_completed"

    asyncio.run(scenario())


def test_wait_task_completed_raises_when_upstream_task_reports_failure() -> None:
    """선행 태스크가 실패로 끝나면 다음 단계로 진행하지 않고 오류를 낸다."""

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), _event_bridge())
        step = CommandStep(
            step_id=1,
            action="WAIT_TASK_COMPLETED",
            params={"task_type": TaskType.DM},
            timeout_sec=1,
        )

        waiter = asyncio.create_task(executor._execute_step(_execute_input(TaskType.ToPP), step))
        await asyncio.sleep(0)

        executor.event_bridge.publish(
            Event(
                event_type=EventType.TASK_COMPLETED,
                item_id=1001,
                payload={"task_type": TaskType.DM, "status": TxnStat.FAIL.value},
            )
        )

        with pytest.raises(RuntimeError, match="Upstream task finished with status=FAIL"):
            await waiter

    asyncio.run(scenario())


def test_wait_task_completed_rejects_missing_item_id() -> None:
    """대기 대상 아이템이 없으면 WAIT_TASK_COMPLETED를 시작하지 않는다."""

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), _event_bridge())
        step = CommandStep(
            step_id=1,
            action="WAIT_TASK_COMPLETED",
            params={"task_type": TaskType.DM},
            timeout_sec=1,
        )

        with pytest.raises(RuntimeError, match="WAIT_TASK_COMPLETED requires item_id"):
            await executor._execute_step(_execute_input(TaskType.ToPP, item_id=None), step)

    asyncio.run(scenario())


def test_wait_subtask_completed_stays_blocked_until_matching_event_arrives() -> None:
    """선행 세부 작업 완료 이벤트가 오기 전까지 멈춰 있다가, 일치하는 이벤트가 오면 다시 진행한다."""

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), _event_bridge())
        step = CommandStep(
            step_id=1,
            action="WAIT_SUBTASK_COMPLETED",
            params={"subtask_type": "pa_dld_done"},
            timeout_sec=1,
        )

        waiter = asyncio.create_task(executor._execute_step(_execute_input(TaskType.ToSTRG), step))
        await asyncio.sleep(0)
        assert not waiter.done()

        executor.event_bridge.publish(
            Event(
                event_type=EventType.SUBTASK_COMPLETED,
                item_id=1001,
                payload={"subtask_type": "pa_dld_done"},
            )
        )

        result = await waiter
        assert result.success is True
        assert result.message == "wait_subtask_completed"

    asyncio.run(scenario())


def test_wait_subtask_completed_returns_immediately_for_preobserved_event() -> None:
    """세부 작업 완료 이벤트를 먼저 받았으면 추가 대기 없이 바로 통과한다."""

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), _event_bridge())
        step = CommandStep(
            step_id=1,
            action="WAIT_SUBTASK_COMPLETED",
            params={"subtask_type": "pa_dld_done"},
            timeout_sec=1,
        )

        executor.event_bridge.publish(
            Event(
                event_type=EventType.SUBTASK_COMPLETED,
                item_id=1001,
                payload={"subtask_type": "pa_dld_done"},
            )
        )
        await asyncio.sleep(0)

        result = await executor._execute_step(_execute_input(TaskType.ToSTRG), step)
        assert result.success is True
        assert result.message == "wait_subtask_completed"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("event_type", "task_type"),
    [
        (EventType.HANDOFF_ACK, TaskType.ToPP),
        (EventType.PP_DONE_REQUESTED, TaskType.PP),
        (EventType.ITEM_LOOKUP_REQUESTED, TaskType.ToINSP),
    ],
)
def test_wait_subtask_completed_resumes_when_matching_external_event_arrives(
    event_type: EventType,
    task_type: TaskType,
) -> None:
    """외부 이벤트가 들어오면 같은 이름으로 기다리던 세부 작업 대기가 풀린다."""

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), _event_bridge())
        step = CommandStep(
            step_id=1,
            action="WAIT_SUBTASK_COMPLETED",
            params={"subtask_type": event_type.value},
            timeout_sec=1,
        )

        waiter = asyncio.create_task(executor._execute_step(_execute_input(task_type), step))
        await asyncio.sleep(0)
        assert not waiter.done()

        executor.event_bridge.publish(
            Event(
                event_type=event_type,
                item_id=1001,
            )
        )

        result = await waiter
        assert result.success is True
        assert result.message == "wait_subtask_completed"

    asyncio.run(scenario())


def test_wait_subtask_completed_rejects_missing_subtask_type() -> None:
    """기다릴 세부 작업 이름이 없으면 WAIT_SUBTASK_COMPLETED를 시작하지 않는다."""

    async def scenario() -> None:
        executor = TaskExecutor(_RecordingAdapter(), _RecordingStateManager(), _event_bridge())
        step = CommandStep(
            step_id=1,
            action="WAIT_SUBTASK_COMPLETED",
            params={},
            timeout_sec=1,
        )

        with pytest.raises(RuntimeError, match="WAIT_SUBTASK_COMPLETED requires params.subtask_type"):
            await executor._execute_step(_execute_input(TaskType.ToSTRG), step)

    asyncio.run(scenario())
