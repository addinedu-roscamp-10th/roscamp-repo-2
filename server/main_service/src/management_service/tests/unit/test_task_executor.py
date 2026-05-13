from __future__ import annotations

import asyncio

from services.contracts.models import AdapterResult, ExecuteTaskInput
from services.contracts.enums import TaskType
from services.core.event_bridge import EventBridgeImpl
from services.core.task_executor import TaskExecutor


class _RecordingAdapter:
    """어댑터 호출 내용을 기록하는 테스트용 객체."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_command(self, res_id: str, action: str, params: dict[str, object]) -> AdapterResult:
        self.calls.append(
            {
                "res_id": res_id,
                "action": action,
                "params": dict(params),
            }
        )
        return AdapterResult(success=True, message="ok")


class _RecordingStateManager:
    """상태 변경, 세부 이벤트 발행, 충전 위치 조회를 기록하는 테스트용 객체."""

    def __init__(self, charger_slot: tuple[int, int] | None = (1, 1)) -> None:
        self.charger_slot = charger_slot
        self.status_updates: list[dict[str, object]] = []
        self.subtask_publications: list[dict[str, object]] = []
        self.charger_requests: list[str | None] = []
        self.charger_return_states: list[dict[str, object]] = []

    async def update_task_status(self, req) -> bool:
        self.status_updates.append(
            {
                "task_id": req.task_id,
                "new_stat": req.new_stat,
                "error_code": req.error_code,
            }
        )
        return True

    async def publish_subtask_completed(
        self,
        *,
        task_id: str,
        item_id: int | None,
        subtask_type: str,
        task_type: TaskType | None = None,
    ) -> bool:
        self.subtask_publications.append(
            {
                "task_id": task_id,
                "item_id": item_id,
                "subtask_type": subtask_type,
                "task_type": task_type,
            }
        )
        return True

    async def get_empty_charger(self, res_id: str | None = None) -> tuple[int, int] | None:
        self.charger_requests.append(res_id)
        return self.charger_slot

    def update_amr_charger_return_state(
        self,
        res_id: str,
        status: str,
        source: str | None = None,
    ) -> None:
        self.charger_return_states.append(
            {
                "res_id": res_id,
                "status": status,
                "source": source,
            }
        )


def _execute_input(
    task_type: TaskType,
    *,
    item_id: str | None = "1001",
    res_id: str = "TAT2",
    payload: dict[str, object] | None = None,
) -> ExecuteTaskInput:
    """TaskExecutor에 넘길 기본 실행 입력을 만든다."""

    return ExecuteTaskInput(
        task_id="task_1",
        res_id=res_id,
        item_id=item_id,
        task_type=task_type,
        payload={} if payload is None else payload,
    )


def _event_bridge() -> EventBridgeImpl:
    """TaskExecutor 테스트에 사용할 기본 이벤트 브리지를 만든다."""

    return EventBridgeImpl()


def test_return_amr_to_charger_marks_toidle_until_arrival() -> None:
    """후처리 충전소 복귀는 출발 시 toidle, 도착 후 idle로 상태를 갱신한다."""

    async def scenario() -> None:
        adapter = _RecordingAdapter()
        state_manager = _RecordingStateManager(charger_slot=(1, 2))
        executor = TaskExecutor(adapter, state_manager, None)

        result = await executor.return_amr_to_charger("TAT2", source="task_88")

        assert result is True
        assert state_manager.charger_requests == ["TAT2"]
        assert state_manager.charger_return_states == [
            {"res_id": "TAT2", "status": "toidle", "source": "task_88"},
            {"res_id": "TAT2", "status": "idle", "source": "task_88"},
        ]
        assert adapter.calls == [
            {
                "res_id": "TAT2",
                "action": "dock_robot",
                "params": {"pose_name": "ToCHG2"},
            }
        ]

    asyncio.run(scenario())
