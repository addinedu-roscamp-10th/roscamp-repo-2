from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from services.contracts.enums import EventType, TaskType, TxnStat
from services.contracts.models import (
    AllocateTaskResInput,
    CreateTaskInput,
    ItemStatusRecord,
    UpdateTaskStatusInput,
)
from services.core.event_bridge import EventBridgeImpl
from services.core.state_manager import StateManager, _make_task_key
from services.core.task_manager import TaskManager


def test_seeded_transport_resources_do_not_use_negative_item_sentinel() -> None:
    state_manager = StateManager(enable_persistence=False)

    assert state_manager._res_list["TAT2"]["item_id"] is None
    assert state_manager._res_list["TAT3"]["item_id"] is None


def test_canonical_task_key_requires_type_and_scopes_same_txn_id() -> None:
    """서로 다른 task type의 같은 txn ID는 별도 canonical key를 사용한다."""
    assert _make_task_key("42", TaskType.ToPP) == "ToPP:42"
    assert _make_task_key("42", TaskType.ToCHG) == "ToCHG:42"

    with pytest.raises(ValueError, match="task_type is required"):
        _make_task_key("42", None)  # type: ignore[arg-type]


def test_update_task_status_input_requires_task_type() -> None:
    """상태 전이는 canonical task identity에 필요한 task_type 없이는 생성할 수 없다."""
    with pytest.raises(ValidationError):
        UpdateTaskStatusInput(task_id="42", new_stat=TxnStat.PROC)

def test_get_empty_start_slot_falls_back_to_memory_slots_when_repo_is_unavailable() -> None:
    """DB 슬롯 조회가 불가해도 메모리 슬롯 테이블로 생산 시작 위치를 계산한다."""

    async def scenario() -> None:
        state_manager = StateManager(enable_persistence=False)
        task_manager = TaskManager(sm=state_manager)
        state_manager.task_manager = task_manager

        start_slot = await state_manager.get_empty_start_slot(target_qty=2)

        assert start_slot == (1, 1)

    asyncio.run(scenario())


def test_get_empty_start_slot_skips_occupied_prefix() -> None:
    """앞 슬롯이 점유돼 있으면 그 다음 연속 빈 구간을 시작 위치로 선택한다."""

    async def scenario() -> None:
        state_manager = StateManager(enable_persistence=False)
        task_manager = TaskManager(sm=state_manager)
        state_manager.task_manager = task_manager
        for col in range(1, 4):
            task_manager.slot_table[(1, col)]["status"] = "occupied"

        start_slot = await state_manager.get_empty_start_slot(target_qty=2)

        assert start_slot == (1, 4)

    asyncio.run(scenario())


def test_get_empty_start_slot_requires_contiguous_slots() -> None:
    """빈 슬롯 수가 충분해도 연속 구간이 아니면 시작 위치를 주지 않는다."""

    async def scenario() -> None:
        state_manager = StateManager(enable_persistence=False)
        task_manager = TaskManager(sm=state_manager)
        state_manager.task_manager = task_manager

        task_manager.slot_table[(1, 5)]["status"] = "occupied"
        task_manager.slot_table[(2, 4)]["status"] = "occupied"

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

        state_manager._tasks["PA_GP:9001"] = {
            "task_id": "PA_GP:9001",
            "task_type": "PA_GP",
            "item_id": 1001,
            "ord_id": 7,
        }
        state_manager.orders[7] = {"target": 2}

        await state_manager.update_task_status(
            UpdateTaskStatusInput(task_id="9001", task_type=TaskType.PA_GP, new_stat=TxnStat.SUCC)
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


def test_task_manager_reserves_db_before_updating_memory_slots() -> None:
    """DB 슬롯을 먼저 예약한 뒤 같은 구간의 메모리 슬롯을 reserved로 바꾼다."""

    class _StateManager:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[int, int], int]] = []
            self.task_manager: TaskManager | None = None
            self.statuses_at_db_call: list[str] = []

        def reserve_storage_slots(
            self,
            start_pos: tuple[int, int],
            target_qty: int,
        ) -> int:
            assert self.task_manager is not None
            self.statuses_at_db_call = [
                self.task_manager.slot_table[(1, col)]["status"]
                for col in range(1, target_qty + 1)
            ]
            self.calls.append((start_pos, target_qty))
            return target_qty

    state_manager = _StateManager()
    task_manager = TaskManager(sm=state_manager)
    state_manager.task_manager = task_manager

    task_manager.reserve_rack_slots(
        order_id=4,
        start_pos=(1, 1),
        target_qty=2,
    )

    assert task_manager.slot_table[(1, 1)]["status"] == "reserved"
    assert task_manager.slot_table[(1, 2)]["status"] == "reserved"
    assert state_manager.calls == [((1, 1), 2)]
    assert state_manager.statuses_at_db_call == ["empty", "empty"]


def test_task_manager_keeps_memory_empty_when_db_reservation_is_incomplete() -> None:
    """DB 예약 수량이 부족하면 메모리 슬롯을 reserved로 바꾸지 않는다."""

    class _StateManager:
        def reserve_storage_slots(
            self,
            start_pos: tuple[int, int],
            target_qty: int,
        ) -> int:
            assert start_pos == (1, 1)
            assert target_qty == 2
            return 0

    task_manager = TaskManager(sm=_StateManager())

    reserved_count = task_manager.reserve_rack_slots(
        order_id=4,
        start_pos=(1, 1),
        target_qty=2,
    )

    assert reserved_count == 0
    assert task_manager.slot_table[(1, 1)] == {
        "status": "empty",
        "order_id": None,
    }
    assert task_manager.slot_table[(1, 2)] == {
        "status": "empty",
        "order_id": None,
    }


def test_task_manager_marks_slot_occupied_after_location_update() -> None:
    """양품 item의 적재 위치를 기록한 뒤 해당 메모리 슬롯을 점유 처리한다."""

    class _StateManager:
        def __init__(self) -> None:
            self.task_manager: TaskManager | None = None
            self.status_at_location_update: str | None = None

        async def update_item_storage_location(
            self,
            item_id: int,
            strg_loc: tuple[int, int],
        ) -> None:
            assert item_id == 1001
            assert strg_loc == (1, 1)
            assert self.task_manager is not None
            self.status_at_location_update = self.task_manager.slot_table[(1, 1)]["status"]

    async def scenario() -> None:
        state_manager = _StateManager()
        task_manager = TaskManager(sm=state_manager)
        state_manager.task_manager = task_manager
        task_manager.slot_table[(1, 1)] = {
            "status": "reserved",
            "order_id": 7,
        }

        strg_loc = await task_manager._calculate_strg_loc(
            TaskType.ToPAWait,
            ItemStatusRecord(item_id=1001, order_id=7),
        )

        assert strg_loc == (1, 1)
        assert state_manager.status_at_location_update == "reserved"
        assert task_manager.slot_table[(1, 1)]["status"] == "occupied"

    asyncio.run(scenario())


def test_complete_ship_batch_persists_before_memory_update() -> None:
    """출고 item과 storage 해제를 DB에 반영한 뒤 메모리 item을 갱신한다."""

    class _Repo:
        def __init__(self) -> None:
            self.state_manager: StateManager | None = None
            self.trace: list[str] = []

        def _assert_memory_not_updated(self) -> None:
            assert self.state_manager is not None
            assert self.state_manager._items[1001]["strg_loc"] == (1, 1)

        def sync_item_snapshot(self, item: dict) -> None:
            self._assert_memory_not_updated()
            assert item["strg_loc"] is None
            self.trace.append("db:item")

        def release_storage_slot_for_item(self, item_id: int) -> bool:
            self._assert_memory_not_updated()
            assert item_id == 1001
            self.trace.append("db:slot")
            return True

    async def scenario() -> None:
        repo = _Repo()
        state_manager = StateManager(repository=repo)
        repo.state_manager = state_manager
        state_manager._items[1001] = {
            "item_id": 1001,
            "flow_stat": "STORED",
            "strg_loc": (1, 1),
        }

        await state_manager.complete_ship_batch([(1001, 1, 1)])

        assert repo.trace == ["db:item", "db:slot"]
        assert state_manager._items[1001]["flow_stat"] == "READY_TO_SHIP"
        assert state_manager._items[1001]["strg_loc"] is None

    asyncio.run(scenario())


def test_task_manager_releases_shipped_slots_for_reuse() -> None:
    """출고가 끝난 슬롯은 다음 주문이 같은 위치를 즉시 예약할 수 있다."""

    class _StateManager:
        def reserve_storage_slots(
            self,
            start_pos: tuple[int, int],
            target_qty: int,
        ) -> int:
            return target_qty

    task_manager = TaskManager(sm=_StateManager())
    task_manager.reserve_rack_slots(2, (1, 1), 3)
    for col in range(1, 4):
        task_manager.slot_table[(1, col)]["status"] = "occupied"

    task_manager.release_shipped_slots(
        [(1, 1, 1), (2, 1, 2), (3, 1, 3)]
    )
    task_manager.reserve_rack_slots(1, (1, 1), 3)

    assert all(
        task_manager.slot_table[(1, col)]
        == {"status": "reserved", "order_id": 1}
        for col in range(1, 4)
    )


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
        # 시나리오 seed의 다른 TAT 상태와 무관하게 TAT1의 가용성만 검증
        state_manager._res_list["TAT1"]["status"] = "IDLE"
        txn_id = await state_manager.insert_task_txn(
            CreateTaskInput(item_id=1001, task_type=TaskType.ToCHG)
        )
        await state_manager.update_task_allocation(
            AllocateTaskResInput(task_id=str(txn_id), task_type=TaskType.ToCHG, item_id=1001, res_id="TAT1")
        )

        await state_manager.update_task_status(
            UpdateTaskStatusInput(
                task_id=str(txn_id),
                task_type=TaskType.ToCHG,
                new_stat=TxnStat.PROC,
            )
        )

        assert state_manager._res_list["TAT1"]["status"] == "CHG"
        assert state_manager._res_list["TAT1"]["task_type"] == TaskType.ToCHG.value
        assert "TAT1" not in await state_manager.get_available_resources("TAT")
        assert published == []

        await state_manager.update_task_status(
            UpdateTaskStatusInput(
                task_id=str(txn_id),
                task_type=TaskType.ToCHG,
                new_stat=TxnStat.SUCC,
            )
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
            AllocateTaskResInput(task_id=str(tochg_txn_id), task_type=TaskType.ToCHG, item_id=1001, res_id="TAT1")
        )
        await state_manager.update_task_status(
            UpdateTaskStatusInput(
                task_id=str(tochg_txn_id),
                task_type=TaskType.ToCHG,
                new_stat=TxnStat.PROC,
            )
        )

        new_txn_id = await state_manager.insert_task_txn(
            CreateTaskInput(item_id=1002, task_type=TaskType.ToPP)
        )
        await state_manager.update_task_allocation(
            AllocateTaskResInput(task_id=str(new_txn_id), task_type=TaskType.ToPP, item_id=1002, res_id="TAT1")
        )

        await state_manager.update_task_status(
            UpdateTaskStatusInput(
                task_id=str(tochg_txn_id),
                task_type=TaskType.ToCHG,
                new_stat=TxnStat.SUCC,
            )
        )

        assert state_manager._res_list["TAT1"]["task_id"] == f"{TaskType.ToPP.value}:{new_txn_id}"
        assert state_manager._res_list["TAT1"]["item_id"] == 1002
        assert state_manager._res_list["TAT1"]["status"] == "ALLOC"

    asyncio.run(scenario())
