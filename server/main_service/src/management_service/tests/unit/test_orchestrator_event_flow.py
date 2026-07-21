from __future__ import annotations

import asyncio

from services.contracts.enums import EventType, TaskType, TxnStat
from services.contracts.models import (
    AllocateTaskInput,
    AllocateTaskResult,
    Event,
    ExecuteTaskInput,
    ExecutionResult,
    ItemStatusRecord,
    NextTaskResult,
    ShipTaskResult,
)
from services.core.event_bridge import EventBridgeImpl
from services.core.orchestrator import Orchestrator


class _RecordingTaskManager:
    """다음 태스크 생성 요청을 기록하고, 이벤트별 응답을 돌려주는 테스트용 객체."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: dict[str | None, list[NextTaskResult]] = {}
        self.ship_calls: list[dict[str, object]] = []
        self.ship_result: ShipTaskResult | None = None
        self.ship_batches: dict[int, list[list[tuple[int, int, int]]]] = {}
        self.reserve_calls: list[dict[str, object]] = []
        self.released_ship_batches: list[list[tuple[int, int, int]]] = []
        self.reserved_count: int | None = None

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

    async def create_ship_task(
        self,
        order_id: int,
        item_locations: list[tuple[int, int, int]],
        event: str | None = None,
    ) -> ShipTaskResult | None:
        self.ship_calls.append(
            {
                "order_id": order_id,
                "item_locations": list(item_locations),
                "event": event,
            }
        )
        return self.ship_result

    def pop_finished_ship_batch(self, order_id: int) -> list[tuple[int, int, int]]:
        batches = self.ship_batches.get(order_id)
        if not batches:
            self.ship_batches.pop(order_id, None)
            return []
        finished_batch = batches.pop(0)
        if not batches:
            self.ship_batches.pop(order_id, None)
        return finished_batch

    def has_ship_plan(self, order_id: int) -> bool:
        return bool(self.ship_batches.get(order_id))

    def log_slot_table(self) -> None:
        return None

    async def reserve_rack_slots(
        self,
        order_id: int,
        start_pos: tuple[int, int],
        target_qty: int,
    ) -> int:
        self.reserve_calls.append(
            {
                "order_id": order_id,
                "start_pos": start_pos,
                "target_qty": target_qty,
            }
        )
        return target_qty if self.reserved_count is None else self.reserved_count

    def release_shipped_slots(self, batch: list[tuple[int, int, int]]) -> None:
        self.released_ship_batches.append(list(batch))


class _SequencedAllocator:
    """할당 결과를 순서대로 반환하고 입력을 기록하는 테스트용 객체."""

    def __init__(self, results: list[AllocateTaskResult]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    async def allocate(self, task: AllocateTaskInput) -> AllocateTaskResult:
        self.calls.append(
            {
                "task_id": task.task_id,
                "item_id": task.item_id,
                "req_res_id": task.req_res_id,
                "task_type": task.task_type,
                "zone_nm": task.zone_nm,
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

    async def execute_task(self, input_data: ExecuteTaskInput) -> ExecutionResult:
        self.execute_calls.append(input_data)
        return ExecutionResult(success=True, task_id=input_data.task_id, final_status="mocked", steps_executed=1)

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
        self.orders: dict[int, dict[str, object]] = {}
        self.start_slots: list[int] = []
        self.completed_ship_batches: list[list[tuple[int, int, int]]] = []
        self.start_production_calls: list[int] = []

    async def get_item(self, item_id: int) -> ItemStatusRecord:
        self.get_item_calls.append(item_id)
        item = self.items[item_id]
        return item.model_copy(deep=True)

    async def get_items_by_order(self, ord_id: int) -> list[ItemStatusRecord]:
        return [
            item.model_copy(deep=True)
            for item in self.items.values()
            if item.order_id == ord_id
        ]

    async def load_ship_item_locations(self, ord_id: int) -> list[tuple[int, int, int]]:
        return sorted(
            (item.item_id, item.strg_loc[0], item.strg_loc[1])
            for item in self.items.values()
            if item.order_id == ord_id and item.strg_loc is not None
        )

    async def complete_ship_batch(self, batch: list[tuple[int, int, int]]) -> None:
        self.completed_ship_batches.append(list(batch))
        for item_id, _, _ in batch:
            item = self.items[item_id]
            self.items[item_id] = item.model_copy(
                update={"flow_stat": "READY_TO_SHIP", "strg_loc": None}
            )

    def is_res_available(self, res_id: str) -> bool:
        return self.available_resources.get(res_id, False)

    async def get_order_target_qty(self, ord_id: int) -> int | None:
        order = self.orders.get(ord_id, {})
        target = order.get("target")
        return int(target) if target is not None else None

    async def get_empty_start_slot(self, target_qty: int) -> tuple[int, int] | None:
        self.start_slots.append(target_qty)
        return (1, 1)

    async def start_production(self, ord_id: int):
        from services.contracts.models import StartProductionOrderAckModel

        self.start_production_calls.append(ord_id)
        return StartProductionOrderAckModel(
            ord_id=ord_id,
            accepted=True,
            reason="ok",
            item_ids=[],
            equip_task_txn_ids=[],
        )


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


def test_start_production_uses_state_manager_target_qty_for_rack_reservation() -> None:
    """랙 예약 수량은 주문 메모리 기본값이 아니라 state manager가 제공한 target qty를 따른다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        allocator = _SequencedAllocator([])
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager({})
        state_manager.orders[3] = {"target": 10}
        orchestrator = Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        result = await orchestrator.start_production([3])

        assert result.accepted_count == 1
        assert state_manager.start_slots == [10]
        assert task_manager.reserve_calls == [
            {
                "order_id": 3,
                "start_pos": (1, 1),
                "target_qty": 10,
            }
        ]

    asyncio.run(scenario())


def test_start_production_rejects_incomplete_slot_reservation() -> None:
    """DB 슬롯 예약 수량이 부족하면 item 생성을 시작하지 않는다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        task_manager.reserved_count = 0
        allocator = _SequencedAllocator([])
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager({})
        state_manager.orders[3] = {"target": 10}
        orchestrator = Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        result = await orchestrator.start_production([3])

        assert result.accepted_count == 0
        assert result.rejected_count == 1
        assert state_manager.start_production_calls == []
        assert result.orders[0].reason == (
            "Rack slot reservation incomplete. target_qty=10 reserved_count=0"
        )

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


def test_start_shipping_collects_item_locations_and_executes_ship_task() -> None:
    """start_shipping은 주문의 적재 위치 목록을 task manager에 넘기고 첫 출고 task를 실행한다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        task_manager.ship_result = ShipTaskResult(
            txn_id=9301,
            priority=7,
            task_type=TaskType.PICK,
            batch=[(1001, 2, 4)],
        )
        allocator = _SequencedAllocator(
            [AllocateTaskResult(success=True, req_res_type="PAT", res_id="PAT1")]
        )
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager(
            {
                1001: _item(item_id=1001, order_id=501, ptn_id=None, strg_loc=(2, 4)),
                1002: _item(item_id=1002, order_id=501, ptn_id=None, strg_loc=(1, 2)),
                2001: _item(item_id=2001, order_id=777, ptn_id=None, strg_loc=(3, 1)),
            }
        )
        orchestrator = Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        started_item_ids = await orchestrator.start_shipping(501)
        await _drain_loop()

        assert started_item_ids == [1001]
        assert task_manager.ship_calls == [
            {
                "order_id": 501,
                "item_locations": [(1001, 2, 4), (1002, 1, 2)],
                "event": "ship_start",
            }
        ]
        assert allocator.calls == [
            {
                "task_id": "9301",
                "item_id": 1001,
                "req_res_id": None,
                "task_type": TaskType.PICK,
                "zone_nm": None,
            }
        ]
        assert len(executor.execute_calls) == 1
        execute_input = executor.execute_calls[0]
        assert execute_input.task_id == "9301"
        assert execute_input.task_type == TaskType.PICK
        assert execute_input.res_id == "PAT1"
        assert execute_input.payload == {
            "item_id": 1001,
            "strg_loc": (2, 4),
            "batch": [(1001, 2, 4)],
        }

    asyncio.run(scenario())


def test_toship_arrival_event_creates_single_pick_with_full_batch_payload() -> None:
    """ToSHIP가 출고 상차 위치에 도착하면 현재 batch 전체를 담은 PICK 1개를 실행한다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        task_manager.ship_result = ShipTaskResult(
            txn_id=9401,
            priority=3,
            task_type=TaskType.PICK,
            batch=[(1001, 2, 4), (1002, 1, 2), (1003, 3, 1)],
        )
        allocator = _SequencedAllocator(
            [AllocateTaskResult(success=True, req_res_type="PAT", res_id="PAT1")]
        )
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager(
            {
                1001: _item(item_id=1001, order_id=501, ptn_id=None, strg_loc=(2, 4)),
                1002: _item(item_id=1002, order_id=501, ptn_id=None, strg_loc=(1, 2)),
                1003: _item(item_id=1003, order_id=501, ptn_id=None, strg_loc=(3, 1)),
            }
        )
        Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        event_bridge.publish(
            Event(
                event_type=EventType.SUBTASK_COMPLETED,
                item_id=1001,
                payload={"task_type": TaskType.ToSHIP, "subtask_type": "toship_src_arrived"},
            )
        )
        await _drain_loop()

        assert task_manager.ship_calls == [
            {
                "order_id": 501,
                "item_locations": [(1001, 2, 4), (1002, 1, 2), (1003, 3, 1)],
                "event": "toship_src_arrived",
            }
        ]
        assert len(executor.execute_calls) == 1
        execute_input = executor.execute_calls[0]
        assert execute_input.task_type == TaskType.PICK
        assert execute_input.item_id == "1001"
        assert execute_input.payload == {
            "item_id": 1001,
            "strg_loc": (2, 4),
            "batch": [(1001, 2, 4), (1002, 1, 2), (1003, 3, 1)],
        }

    asyncio.run(scenario())


def test_pick_completed_event_creates_next_shipping_task() -> None:
    """출고 PICK 완료 이벤트는 현재 batch를 끝내고 다음 ToSHIP을 만든다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _RecordingTaskManager()
        task_manager.ship_result = ShipTaskResult(
            txn_id=9501,
            priority=3,
            task_type=TaskType.ToSHIP,
            batch=[(1004, 2, 5)],
        )
        task_manager.ship_batches[501] = [
            [(1001, 2, 4)],
            [(1004, 2, 5)],
        ]
        allocator = _SequencedAllocator(
            [AllocateTaskResult(success=True, req_res_type="TAT", res_id="TAT1")]
        )
        executor = _RecordingExecutor()
        state_manager = _RecordingStateManager(
            {
                1001: _item(item_id=1001, order_id=501, ptn_id=None, strg_loc=(2, 4)),
                1004: _item(item_id=1004, order_id=501, ptn_id=None, strg_loc=(2, 5)),
            }
        )
        Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        event_bridge.publish(
            Event(
                event_type=EventType.TASK_COMPLETED,
                item_id=1001,
                payload={"task_type": TaskType.PICK, "status": TxnStat.SUCC.value},
            )
        )
        await _drain_loop()

        assert task_manager.calls == []
        assert task_manager.ship_calls == [
            {
                "order_id": 501,
                "item_locations": [(1004, 2, 5)],
                "event": "pick_done",
            }
        ]
        assert state_manager.completed_ship_batches == [[(1001, 2, 4)]]
        assert task_manager.released_ship_batches == [[(1001, 2, 4)]]
        assert len(executor.execute_calls) == 1
        execute_input = executor.execute_calls[0]
        assert execute_input.task_type == TaskType.ToSHIP
        assert execute_input.item_id == "1004"
        assert execute_input.payload == {
            "item_id": 1004,
            "strg_loc": (2, 5),
            "batch": [(1004, 2, 5)],
        }

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


def test_task_completed_event_burst_schedules_all_follow_up_tasks() -> None:
    """여러 TASK_COMPLETED 이벤트가 한 번에 들어와도 후속 작업을 빠뜨리지 않는다."""

    class _BurstTaskManager(_RecordingTaskManager):
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
            await asyncio.sleep(0)
            return [
                NextTaskResult(
                    item_id=item_info.item_id,
                    txn_id=9000 + item_info.item_id,
                    task_type=TaskType.ToINSP,
                    priority=5,
                )
            ]

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        task_manager = _BurstTaskManager()
        allocator = _SequencedAllocator(
            [
                AllocateTaskResult(success=True, req_res_type="TAT", res_id="TAT2")
                for _ in range(12)
            ]
        )
        executor = _RecordingExecutor()
        items = {
            item_id: _item(item_id=item_id, order_id=500 + item_id)
            for item_id in range(1001, 1013)
        }
        state_manager = _RecordingStateManager(items)
        Orchestrator(task_manager, allocator, state_manager, event_bridge, executor)

        for item_id in items:
            event_bridge.publish(
                Event(
                    event_type=EventType.TASK_COMPLETED,
                    item_id=item_id,
                    payload={"task_type": TaskType.PP, "status": TxnStat.SUCC.value},
                )
            )

        for _ in range(5):
            await asyncio.sleep(0)

        assert len(task_manager.calls) == len(items)
        assert len(allocator.calls) == len(items)
        assert len(executor.execute_calls) == len(items)
        assert {call.item_id for call in executor.execute_calls} == {str(item_id) for item_id in items}

    asyncio.run(scenario())
