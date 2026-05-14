from __future__ import annotations

import asyncio

from services.contracts.enums import EventType, TaskType, TxnStat
from services.contracts.models import AllocateTaskResInput, CreateTaskInput, UpdateTaskStatusInput
from services.core.event_bridge import EventBridgeImpl
from services.core.state_manager import StateManager
from services.core.task_manager import TaskManager


def test_seeded_transport_resources_do_not_use_negative_item_sentinel() -> None:
    state_manager = StateManager(enable_persistence=False)

    assert state_manager._res_list["TAT2"]["item_id"] is None
    assert state_manager._res_list["TAT3"]["item_id"] is None


def test_get_empty_start_slot_falls_back_to_memory_slots_when_repo_is_unavailable() -> None:
    """DB 슬롯 조회가 불가해도 메모리 슬롯 테이블로 생산 시작 위치를 계산한다."""

    async def scenario() -> None:
        state_manager = StateManager(enable_persistence=False)
        task_manager = TaskManager(sm=state_manager)
        state_manager.task_manager = task_manager

        start_slot = await state_manager.get_empty_start_slot(target_qty=2)

        assert start_slot == (1, 4)

    asyncio.run(scenario())


def test_get_empty_start_slot_requires_contiguous_slots() -> None:
    """빈 슬롯 수가 충분해도 연속 구간이 아니면 시작 위치를 주지 않는다."""

    async def scenario() -> None:
        state_manager = StateManager(enable_persistence=False)
        task_manager = TaskManager(sm=state_manager)
        state_manager.task_manager = task_manager

        task_manager.slot_table[(1, 5)]["status"] = "Used"
        task_manager.slot_table[(2, 4)]["status"] = "Used"

        start_slot = await state_manager.get_empty_start_slot(target_qty=9)

        assert start_slot is None

    asyncio.run(scenario())


def test_update_item_storage_location_stores_tuple_in_memory() -> None:
    """적재 위치는 메모리에서 row-col 문자열 대신 tuple로 유지한다."""

    async def scenario() -> None:
        state_manager = StateManager(enable_persistence=False)

        await state_manager.update_item_storage_location(1001, (2, 4))
        item = await state_manager.get_item(1001)

        assert item.strg_loc == (2, 4)

    asyncio.run(scenario())


def test_update_item_storage_location_defers_repository_write_until_pa_gp_success() -> None:
    """적재 위치는 먼저 메모리에 저장하고 DB 점유 반영은 PA_GP 성공 때 수행한다."""

    class _Repo:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int]] = []

        def update_item_storage_location(self, item_id: int, row: int, col: int) -> None:
            self.calls.append((item_id, row, col))

    async def scenario() -> None:
        repo = _Repo()
        state_manager = StateManager(repository=repo)

        await state_manager.update_item_storage_location(1001, (2, 4))

        assert repo.calls == []

        state_manager._tasks["task_9001"] = {
            "task_id": "task_9001",
            "task_type": "PA_GP",
            "item_id": 1001,
            "ord_id": 7,
        }
        state_manager.orders[7] = {"target": 2}

        await state_manager.update_task_status(
            UpdateTaskStatusInput(task_id="9001", new_stat=TxnStat.SUCC)
        )

        assert repo.calls == [(1001, 2, 4)]

    asyncio.run(scenario())


def test_get_order_target_qty_prefers_repository_and_syncs_memory() -> None:
    """DB 모드에서는 주문 수량을 repo에서 읽고 메모리 order 상태에도 반영한다."""

    class _Repo:
        def get_order_target_qty(self, ord_id: int) -> int | None:
            assert ord_id == 3
            return 10

    async def scenario() -> None:
        state_manager = StateManager(repository=_Repo())

        target_qty = await state_manager.get_order_target_qty(3)

        assert target_qty == 10
        assert state_manager.orders[3]["target"] == 10

    asyncio.run(scenario())


def test_reserve_storage_slots_passes_start_position_and_quantity_to_repository() -> None:
    """랙 예약 sync는 시작 row-col과 예약 수량을 repository에 그대로 넘긴다."""

    class _Repo:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int]] = []

        def reserve_storage_slots(self, row: int, col: int, target_qty: int) -> int:
            self.calls.append((row, col, target_qty))
            return target_qty

    repo = _Repo()
    state_manager = StateManager(repository=repo)

    reserved_count = state_manager.reserve_storage_slots((1, 2), 3)

    assert reserved_count == 3
    assert repo.calls == [(1, 2, 3)]


def test_task_manager_reservation_syncs_reserved_slots_to_state_manager() -> None:
    """메모리 랙 예약이 성공하면 같은 구간을 state manager에 DB sync 요청한다."""

    class _StateManager:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[int, int], int]] = []

        def reserve_storage_slots(
            self,
            start_pos: tuple[int, int],
            target_qty: int,
        ) -> int:
            self.calls.append((start_pos, target_qty))
            return target_qty

    state_manager = _StateManager()
    task_manager = TaskManager(sm=state_manager)

    task_manager.reserve_rack_slots(order_id=4, start_pos=(1, 1), target_qty=2)

    assert task_manager.slot_table[(1, 1)]["status"] == "reserved"
    assert task_manager.slot_table[(1, 2)]["status"] == "reserved"
    assert state_manager.calls == [((1, 1), 2)]


def test_tochg_keeps_amr_unavailable_until_charged() -> None:
    """ToCHG는 무조건 배터리 부족 AMR이 충전소로 가는 작업이므로,
    충전 완료 전까지 가용 자원 후보에 포함되지 않는다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        published: list[tuple[str | None, str | None]] = []
        event_bridge.subscribe(
            EventType.RESOURCE_AVAILABLE,
            lambda event: published.append((event.res_id, event.payload.get("status"))),
            "test.tochg.resource_available",
        )

        state_manager = StateManager(event_bridge=event_bridge, enable_persistence=False)
        # 시나리오 seed 의 다른 TAT 상태와 무관하게 TAT1 의 가용성만 검증
        state_manager._res_list["TAT1"]["status"] = "IDLE"
        txn_id = await state_manager.insert_task_txn(
            CreateTaskInput(item_id=1001, task_type=TaskType.ToCHG)
        )
        await state_manager.update_task_allocation(
            AllocateTaskResInput(task_id=str(txn_id), item_id=1001, res_id="TAT1")
        )

        await state_manager.update_task_status(
            UpdateTaskStatusInput(task_id=str(txn_id), new_stat=TxnStat.PROC)
        )

        assert state_manager._res_list["TAT1"]["status"] == "CHG"
        assert state_manager._res_list["TAT1"]["task_type"] == TaskType.ToCHG.value
        assert "TAT1" not in await state_manager.get_available_resources("TAT")
        assert published == []

        await state_manager.update_task_status(
            UpdateTaskStatusInput(task_id=str(txn_id), new_stat=TxnStat.SUCC)
        )

        assert state_manager._res_list["TAT1"]["status"] == "CHG"
        assert "TAT1" not in await state_manager.get_available_resources("TAT")
        assert published == []

        await state_manager.publish_amr_charged(res_id="TAT1", task_id=str(txn_id))

        assert state_manager._res_list["TAT1"]["status"] == "IDLE"
        assert "TAT1" in await state_manager.get_available_resources("TAT")

    asyncio.run(scenario())


def test_tochg_completion_does_not_overwrite_newer_resource_assignment() -> None:
    """충전 복귀 task가 늦게 끝나도 그 사이 생긴 새 배정을 덮어쓰지 않아야 한다."""

    async def scenario() -> None:
        state_manager = StateManager(enable_persistence=False)
        tochg_txn_id = await state_manager.insert_task_txn(
            CreateTaskInput(item_id=1001, task_type=TaskType.ToCHG)
        )
        await state_manager.update_task_allocation(
            AllocateTaskResInput(task_id=str(tochg_txn_id), item_id=1001, res_id="TAT1")
        )
        await state_manager.update_task_status(
            UpdateTaskStatusInput(task_id=str(tochg_txn_id), new_stat=TxnStat.PROC)
        )

        new_txn_id = await state_manager.insert_task_txn(
            CreateTaskInput(item_id=1002, task_type=TaskType.ToPP)
        )
        await state_manager.update_task_allocation(
            AllocateTaskResInput(task_id=str(new_txn_id), item_id=1002, res_id="TAT1")
        )

        await state_manager.update_task_status(
            UpdateTaskStatusInput(task_id=str(tochg_txn_id), new_stat=TxnStat.SUCC)
        )

        assert state_manager._res_list["TAT1"]["task_id"] == str(new_txn_id)
        assert state_manager._res_list["TAT1"]["item_id"] == 1002
        assert state_manager._res_list["TAT1"]["status"] == "ALLOC"

    asyncio.run(scenario())
