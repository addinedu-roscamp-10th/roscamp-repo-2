from __future__ import annotations

import asyncio

from services.contracts.enums import EventType, TaskType, TxnStat
from services.contracts.models import AllocateTaskResInput, CreateTaskInput, UpdateTaskStatusInput
from services.core.event_bridge import EventBridgeImpl
from services.core.mock_state_manager import MockStateManager


def test_tochg_proc_marks_amr_as_toidle_and_releases_it_for_allocation() -> None:
    """ToCHG가 PROC로 들어가면 AMR은 toidle 상태로 풀려 재할당 가능해야 한다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        published: list[tuple[str | None, str | None]] = []
        event_bridge.subscribe(
            EventType.RESOURCE_AVAILABLE,
            lambda event: published.append((event.res_id, event.payload.get("status"))),
            "test.tochg.resource_available",
        )

        state_manager = MockStateManager(event_bridge=event_bridge, enable_persistence=False)
        txn_id = await state_manager.insert_task_txn(
            CreateTaskInput(item_id=1001, task_type=TaskType.ToCHG)
        )
        await state_manager.update_task_allocation(
            AllocateTaskResInput(task_id=str(txn_id), item_id=1001, res_id="TAT1")
        )

        await state_manager.update_task_status(
            UpdateTaskStatusInput(task_id=str(txn_id), new_stat=TxnStat.PROC)
        )

        assert state_manager._res_list["TAT1"]["status"] == "toidle"
        assert state_manager._res_list["TAT1"]["item_id"] is None
        assert await state_manager.get_available_resources("TAT") == ["TAT1"]
        assert published == [("TAT1", "toidle")]

    asyncio.run(scenario())

def test_battery_low_tochg_proc_keeps_amr_unavailable_until_charged() -> None:
    """BAT_LOW 기원의 ToCHG는 충전 완료 전까지 가용 자원 후보에 포함되지 않는다."""

    async def scenario() -> None:
        event_bridge = EventBridgeImpl()
        published: list[tuple[str | None, str | None]] = []
        event_bridge.subscribe(
            EventType.RESOURCE_AVAILABLE,
            lambda event: published.append((event.res_id, event.payload.get("status"))),
            "test.battery_low_tochg.resource_available",
        )

        state_manager = MockStateManager(event_bridge=event_bridge, enable_persistence=False)
        state_manager._res_list["TAT1"]["condition"] = "BATTERY_LOW"
        txn_id = await state_manager.insert_task_txn(
            CreateTaskInput(
                item_id=1001,
                task_type=TaskType.ToCHG,
            )
        )
        await state_manager.update_task_allocation(
            AllocateTaskResInput(task_id=str(txn_id), item_id=1001, res_id="TAT1")
        )

        await state_manager.update_task_status(
            UpdateTaskStatusInput(task_id=str(txn_id), new_stat=TxnStat.PROC)
        )

        assert state_manager._res_list["TAT1"]["status"] == "charging"
        assert state_manager._res_list["TAT1"]["task_type"] == TaskType.ToCHG.value
        assert await state_manager.get_available_resources("TAT") == []
        assert published == []

        await state_manager.update_task_status(
            UpdateTaskStatusInput(task_id=str(txn_id), new_stat=TxnStat.SUCC)
        )

        assert state_manager._res_list["TAT1"]["status"] == "charging"
        assert await state_manager.get_available_resources("TAT") == []
        assert published == []

        await state_manager.publish_amr_charged(res_id="TAT1", task_id=str(txn_id))

        assert state_manager._res_list["TAT1"]["status"] == "idle"
        assert await state_manager.get_available_resources("TAT") == ["TAT1"]

    asyncio.run(scenario())


def test_tochg_completion_does_not_overwrite_newer_resource_assignment() -> None:
    """충전 복귀 task가 늦게 끝나도 그 사이 생긴 새 배정을 덮어쓰지 않아야 한다."""

    async def scenario() -> None:
        state_manager = MockStateManager(enable_persistence=False)
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
        assert state_manager._res_list["TAT1"]["status"] == "allocated"

    asyncio.run(scenario())
