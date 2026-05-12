from __future__ import annotations

import asyncio

from services.contracts.enums import EventType, TaskType, TxnStat
from services.contracts.models import (
    AllocateTaskResult,
    Event,
    ExecuteTaskInput,
    ItemStatusRecord,
    NextTaskResult,
)
from services.core.event_bridge import EventBridgeImpl
from services.core.orchestrator import Orchestrator


class _RecordingTaskManager:
    """다음 태스크 생성 요청을 기록하고, 이벤트별 응답을 돌려주는 테스트용 객체."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: dict[str | None, list[NextTaskResult]] = {}

    async def create_next_task(
        self,
        item_info: ItemStatusRecord,
        event: str | None = None,
    ) -> list[NextTaskResult]:
        self.calls.append(
            {
                "item_id": item_info.item_id,
                "order_id": item_info.order_id,
                "last_task_type": item_info.last_task_type,
                "planning_event": event,
                "req_res_id": item_info.req_res_id,
                "strg_loc": item_info.strg_loc,
                "ptn_id": item_info.ptn_id,
            }
        )
        return list(self.responses.get(event, []))


class _SequencedAllocator:
    """할당 결과를 순서대로 반환하고 입력을 기록하는 테스트용 객체."""

    def __init__(self, results: list[AllocateTaskResult]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    async def allocate(self, input_data) -> AllocateTaskResult:
        self.calls.append(
            {
                "task_id": input_data.task_id,
                "item_id": input_data.item_id,
                "req_res_id": input_data.req_res_id,
                "task_type": input_data.task_type,
                "zone_nm": input_data.zone_nm,
            }
        )
        if self.results:
            return self.results.pop(0)
        return AllocateTaskResult(success=False, req_res_type="TAT", reason="no_result")


class _RecordingExecutor:
    """실행 요청과 비상 복구 요청을 기록하는 테스트용 객체."""

    def __init__(self) -> None:
        self.execute_calls: list[ExecuteTaskInput] = []
        self.emergency_returns: list[dict[str, object]] = []
        self.charger_returns: list[dict[str, object]] = []

    async def execute_task(self, input_data: ExecuteTaskInput) -> None:
        self.execute_calls.append(input_data)

    async def handle_emergency_return(self, item_id: int, amr_id: str, arm_id: str) -> None:
        self.emergency_returns.append(
            {
                "item_id": item_id,
                "amr_id": amr_id,
                "arm_id": arm_id,
            }
        )

    async def return_amr_to_charger(self, res_id: str, source: str | None = None) -> bool:
        self.charger_returns.append({"res_id": res_id, "source": source})
        return True


class _RecordingStateManager:
    """아이템 조회 요청만 기록하고 미리 준비한 아이템 상태를 돌려주는 테스트용 객체."""

    def __init__(self, items: dict[int, ItemStatusRecord]) -> None:
        self.items = items
        self.get_item_calls: list[int] = []
        self.available_resources: dict[str, bool] = {}

    async def get_item(self, item_id: int) -> ItemStatusRecord:
        self.get_item_calls.append(item_id)
        item = self.items[item_id]
        return item.model_copy(deep=True)

    def is_res_available(self, res_id: str) -> bool:
        return self.available_resources.get(res_id, False)


def _item(
    *,
    item_id: int = 1001,
    order_id: int = 501,
    req_res_id: str | None = None,
    ptn_id: int | None = 7,
    strg_loc: tuple[int, int] | None = (2, 4),
) -> ItemStatusRecord:
    """오케스트레이터 테스트에 사용할 기본 아이템 상태를 만든다."""

    return ItemStatusRecord(
        item_id=item_id,
        order_id=order_id,
        req_res_id=req_res_id,
        ptn_id=ptn_id,
        strg_loc=strg_loc,
    )


async def _drain_loop() -> None:
    """이벤트 브리지와 create_task로 예약된 작업이 모두 실행되도록 루프를 잠깐 비운다."""

    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_task_completed_event_creates_allocates_and_executes_next_task() -> None:
    """성공한 TASK_COMPLETED 이벤트를 받으면 다음 태스크를 만들고 실행까지 연결한다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        task_manager.responses[None] = [
            NextTaskResult(item_id=1001, txn_id=9101, task_type=TaskType.ToINSP, priority=8)
        ]
        allocator = _SequencedAllocator(
            [AllocateTaskResult(success=True, res_id="TAT2", req_res_type="TAT")]
        )
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager({1001: _item()})
        Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        event_bridge.publish(
            Event(
                event_type=EventType.TASK_COMPLETED,
                item_id=1001,
                payload={"task_type": TaskType.PP, "status": TxnStat.SUCC.value},
            )
        )
        await _drain_loop()

        assert task_manager.calls == [
            {
                "item_id": 1001,
                "order_id": 501,
                "last_task_type": TaskType.PP,
                "planning_event": None,
                "req_res_id": None,
                "strg_loc": (2, 4),
                "ptn_id": 7,
            }
        ]
        assert allocator.calls == [
            {
                "task_id": "9101",
                "item_id": 1001,
                "req_res_id": None,
                "task_type": TaskType.ToINSP,
                "zone_nm": None,
            }
        ]
        assert len(executor.execute_calls) == 1
        execute_input = executor.execute_calls[0]
        assert execute_input.task_id == "9101"
        assert execute_input.res_id == "TAT2"
        assert execute_input.task_type == TaskType.ToINSP
        assert execute_input.item_id == "1001"
        assert execute_input.payload == {
            "item_id": 1001,
            "ptn_loc_id": 7,
            "strg_loc": (2, 4),
        }

    asyncio.run(scenario())


def test_failed_task_completed_event_does_not_plan_any_follow_up_task() -> None:
    """실패로 끝난 TASK_COMPLETED 이벤트는 다음 태스크 계획으로 이어지지 않는다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        allocator = _SequencedAllocator([])
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager({1001: _item()})
        Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        event_bridge.publish(
            Event(
                event_type=EventType.TASK_COMPLETED,
                item_id=1001,
                payload={"task_type": TaskType.PP, "status": TxnStat.FAIL.value},
            )
        )
        await _drain_loop()

        assert task_manager.calls == []
        assert allocator.calls == []
        assert executor.execute_calls == []

    asyncio.run(scenario())


def test_subtask_completed_event_passes_planning_event_to_task_manager() -> None:
    """SUBTASK_COMPLETED 이벤트는 세부 작업 이름을 planning_event로 넘긴다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        allocator = _SequencedAllocator([])
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager({1001: _item()})
        Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        event_bridge.publish(
            Event(
                event_type=EventType.SUBTASK_COMPLETED,
                item_id=1001,
                payload={"task_type": TaskType.ToSTRG, "subtask_type": "tostrg_dld"},
            )
        )
        await _drain_loop()

        assert task_manager.calls == [
            {
                "item_id": 1001,
                "order_id": 501,
                "last_task_type": TaskType.ToSTRG,
                "planning_event": "tostrg_dld",
                "req_res_id": None,
                "strg_loc": (2, 4),
                "ptn_id": 7,
            }
        ]

    asyncio.run(scenario())


def test_resource_available_event_retries_pending_task_and_executes_it() -> None:
    """대기열에 들어간 작업은 RESOURCE_AVAILABLE 이벤트가 오면 다시 할당을 시도한다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        allocator = _SequencedAllocator(
            [
                AllocateTaskResult(success=False, req_res_type="TAT", reason="busy"),
                AllocateTaskResult(success=True, req_res_type="TAT", res_id="TAT3"),
            ]
        )
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager({1001: _item(req_res_id="TAT3")})
        orchestrator = Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        await orchestrator._allocate_and_execute(
            NextTaskResult(item_id=1001, txn_id=9201, task_type=TaskType.ToINSP, priority=5),
            _item(req_res_id="TAT3"),
        )
        assert executor.execute_calls == []

        event_bridge.publish(
            Event(
                event_type=EventType.RESOURCE_AVAILABLE,
                res_id="TAT3",
                payload={"req_res_type": "TAT"},
            )
        )
        await _drain_loop()

        assert allocator.calls == [
            {
                "task_id": "9201",
                "item_id": 1001,
                "req_res_id": "TAT3",
                "task_type": TaskType.ToINSP,
                "zone_nm": None,
            },
            {
                "task_id": "9201",
                "item_id": 1001,
                "req_res_id": "TAT3",
                "task_type": TaskType.ToINSP,
                "zone_nm": None,
            },
        ]
        assert len(executor.execute_calls) == 1
        assert executor.execute_calls[0].res_id == "TAT3"
        assert executor.charger_returns == []

    asyncio.run(scenario())


def test_resource_available_event_sends_idle_tat_to_charger_when_no_pending_task_uses_it() -> None:
    """RESOURCE_AVAILABLE 이후에도 TAT가 비어 있으면 오케스트레이터가 충전소 복귀를 지시한다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        allocator = _SequencedAllocator([])
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager({1001: _item()})
        state_manager.available_resources["TAT3"] = True
        Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        event_bridge.publish(
            Event(
                event_type=EventType.RESOURCE_AVAILABLE,
                res_id="TAT3",
                payload={"req_res_type": "TAT", "task_id": "task_88"},
            )
        )
        await _drain_loop()

        assert allocator.calls == []
        assert executor.execute_calls == []
        assert executor.charger_returns == [{"res_id": "TAT3", "source": "task_88"}]

    asyncio.run(scenario())


def test_amr_battery_low_event_requests_emergency_return() -> None:
    """AMR_BATTERY_LOW 이벤트를 받으면 실행기는 비상 복구 시퀀스를 시작한다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        allocator = _SequencedAllocator([])
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager({1001: _item()})
        Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        event_bridge.publish(
            Event(
                event_type=EventType.AMR_BATTERY_LOW,
                item_id=1001,
                res_id="TAT2",
                payload={"arm_id": "MAT1", "amr_id": "TAT2"},
            )
        )
        await _drain_loop()

        assert executor.emergency_returns == [
            {
                "item_id": 1001,
                "amr_id": "TAT2",
                "arm_id": "MAT1",
            }
        ]

    asyncio.run(scenario())


def test_arm_return_completed_event_schedules_recovery_and_charge_tasks() -> None:
    """ARM_RETURN_COMPLETED 이벤트를 받으면 복귀 작업과 충전 작업을 각각 계획한다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        task_manager.responses["amr_battery_low_ToPP"] = [
            NextTaskResult(item_id=1001, txn_id=9301, task_type=TaskType.ToPP, priority=15)
        ]
        task_manager.responses["amr_battery_low_ToCHG"] = [
            NextTaskResult(item_id=1001, txn_id=9302, task_type=TaskType.ToCHG, priority=10)
        ]
        allocator = _SequencedAllocator(
            [
                AllocateTaskResult(success=True, req_res_type="TAT", res_id="TAT2"),
                AllocateTaskResult(success=True, req_res_type="TAT", res_id="TAT2"),
            ]
        )
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager({1001: _item()})
        Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        event_bridge.publish(
            Event(
                event_type=EventType.ARM_RETURN_COMPLETED,
                item_id=1001,
                res_id="TAT2",
                payload={"arm_id": "MAT1"},
            )
        )
        await _drain_loop()

        assert task_manager.calls == [
            {
                "item_id": 1001,
                "order_id": 501,
                "last_task_type": None,
                "planning_event": "amr_battery_low_ToPP",
                "req_res_id": None,
                "strg_loc": (2, 4),
                "ptn_id": 7,
            },
            {
                "item_id": 1001,
                "order_id": 501,
                "last_task_type": None,
                "planning_event": "amr_battery_low_ToCHG",
                "req_res_id": None,
                "strg_loc": (2, 4),
                "ptn_id": 7,
            },
        ]
        assert allocator.calls == [
            {
                "task_id": "9301",
                "item_id": 1001,
                "req_res_id": None,
                "task_type": TaskType.ToPP,
                "zone_nm": None,
            },
            {
                "task_id": "9302",
                "item_id": 1001,
                "req_res_id": "TAT2",
                "task_type": TaskType.ToCHG,
                "zone_nm": None,
            },
        ]
        assert [call.task_type for call in executor.execute_calls] == [TaskType.ToPP, TaskType.ToCHG]

    asyncio.run(scenario())
