"""Event-driven orchestrator for Management production start flows."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.contracts.models import (
    StartProductionBatchAckModel,
    StartProductionOrderAckModel,
    ItemStatusRecord,
    NextTaskResult,
    TaskExecutorInput,
    AllocateTaskInput,
)
from services.contracts.enums import TaskType
from services.contracts.enums import EventType
from services.contracts.protocols import IOrchestrator


if TYPE_CHECKING:
    from services.contracts.protocols import ITaskManager
    from services.contracts.protocols import ITaskAllocator
    from services.contracts.protocols import ITaskExecutor
    from services.contracts.protocols import IEventBridge
    from services.contracts.protocols import IStateManager

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class _PendingTask:
    task: NextTaskResult
    item_info: ItemStatusRecord

class Orchestrator(IOrchestrator):
    """Coordinates production-start entrypoints onto one async control plane."""

    def __init__(
        self,
        task_manager: ITaskManager,
        task_allocator: ITaskAllocator,
        state_manager: IStateManager,
        event_bridge: IEventBridge,
        task_executor: ITaskExecutor | None = None,
    ) -> None:
        self.task_manager = task_manager
        self.task_allocator = task_allocator
        self.task_executor = task_executor
        self.state_manager = state_manager
        self.event_bridge = event_bridge

        self.event_bridge.subscribe(
            EventType.TASK_COMPLETED,
            self.on_task_completed,
            "orchestrator.task_completed",
        )
        self.event_bridge.subscribe(
            EventType.SUBTASK_COMPLETED,
            self.on_subtask_completed,
            "orchestrator.subtask_completed",
        )
        self.event_bridge.subscribe(
            EventType.ITEM_STATUS_CHANGED,
            self.on_item_status_changed,
            "orchestrator.item_status_changed",
        )
        self.event_bridge.subscribe(
            EventType.AMR_CHARGED,
            self.on_amr_charged,
            "orchestrator.amr_charged",
        )
        self.event_bridge.subscribe(
            EventType.AMR_BATTERY_LOW,
            self.on_amr_battery_low,
            "orchestrator.amr_battery_low",
        )

        self._pending_tasks: list[_PendingTask] = []

    async def start_production(self, ord_ids: list[int]) -> "StartProductionBatchAckModel":
        """StateManager를 통해 지정된 주문들의 생산 시작을 처리."""
        logger.info("주문에 대한 생산 시작: ord_ids=%s", ord_ids)
        results: list[StartProductionOrderAckModel] = []
        accepted = 0
        rejected = 0

        for ord_id in ord_ids:
            try:
                result = await self.state_manager.start_production(ord_id)
            except Exception as exc:
                logger.warning("생산 시작 실패: ord_id=%s reason=%s", ord_id, exc)
                result = self._build_rejected_ack(ord_id, str(exc))

            if result.accepted:
                accepted += 1
                for item_id in result.item_ids:
                    await self._schedule_next_task(item_id)
            else:
                rejected += 1

            results.append(result)

        batch_result = self._build_batch_ack(ord_ids, results, accepted, rejected)
        logger.info(
            "생산 시작 완료: requested=%d, accepted=%d",
            batch_result.requested_count,
            batch_result.accepted_count,
        )
        return batch_result

    async def start_shipping(self, ord_ids: int | None = None) -> list[int]:  # 출고 구현할 때 필요
        """SHIP event handler."""
        return []

    def _build_rejected_ack(self, ord_id: int, reason: str) -> "StartProductionOrderAckModel":
        from ..contracts.models import StartProductionOrderAckModel

        return StartProductionOrderAckModel(
            ord_id=ord_id,
            accepted=False,
            reason=reason,
        )

    def _build_batch_ack(
        self,
        ord_ids: list[int],
        results: list["StartProductionOrderAckModel"],
        accepted: int,
        rejected: int,
    ) -> "StartProductionBatchAckModel":
        from ..contracts.models import StartProductionBatchAckModel

        return StartProductionBatchAckModel(
            requested_count=len(ord_ids),
            accepted_count=accepted,
            rejected_count=rejected,
            orders=results,
            message="DB-backed start_production completed." if accepted else "No orders were accepted.",
        )

    async def _schedule_next_task(self, item_id: int, event: str | None = None) -> None:
        item_info = self._build_item_status_record(self.state_manager.get_item(item_id))
        next_tasks = self.task_manager.create_next_task(item_info, event)

        if not next_tasks:
            logger.info("no next task planned: item_id=%s", item_id)
            return

        for next_task in next_tasks:
            await self._allocate_and_execute(next_task, item_info)

    # task가 끝났을 때는 다음 task 생성, 할당, 실행
    async def on_task_completed(self, event) -> None:
        """TASK_COMPLETED event handler."""
        await self._schedule_next_task(event.item_id)

    # 세부 공정 완료 시 작업
    async def on_subtask_completed(self, event) -> None:
        """중간 공정 완료 이벤트 처리."""
        subtask = event.payload.get("subtask")
        if subtask is None:
            logger.info("subtask completed event without subtask payload: %s", event)
            return

        logger.info(
            "subtask completed: task_id=%s item_id=%s subtask=%s",
            event.payload.get("task_id"),
            event.item_id,
            subtask,
        )
        await self._schedule_next_task(event.item_id, subtask)

    # amr 배터리가 충전되었을 때, pending task들 할당 시도
    async def on_amr_charged(self, event) -> None:
        """AMR_CHARGED event handler."""
        res_id = getattr(event, "res_id", None)
        logger.info("amr charged: res_id=%s", res_id)
        await self._process_pending_tasks()

    # item 상태 변경 시에 ui 변경 등(monitoring manager?)
    async def on_item_status_changed(self, event) -> None:
        """ITEM_STATUS_CHANGED event handler."""
        logger.debug("item status changed: item_id=%s", getattr(event, "item_id", None))

    # State manager는 battery low를 감지했을 때, 해당 로봇에 대한 정보(어떤 item_id를 들고 있는지)를 조회한 후에 event에 담아서 보내야함.
    # 근데 우리는 wait_ld에서만 bat low가 뜨는 것으로 가정하기 때문에 아마 항상 없을 것
    async def on_amr_battery_low(self, event) -> None:
        item_id = getattr(event, "item_id", None)
        if item_id is None: # 이미 charging zone으로 가고 있을 때는 item id가 없음
            logger.warning("battery low event without item_id: %s", event)
            return

        res_id = getattr(event, "res_id", None)
        logger.warning(
            "battery low event received: res_id=%s item_id=%s",
            res_id,
            item_id,
        )
        item_info = self._build_item_status_record(self.state_manager.get_item(item_id))
        
        
        chg_tasks = self.task_manager.create_next_task(item_info, "amr_battery_low")
        chg_task = chg_tasks[0]
        allocate_input = self._build_allocate_task_input(chg_task, item_info)
        self.task_allocator.update_task_allocation(allocate_input, res_id)
        execute_input = TaskExecutorInput(
            task_id=str(chg_task.txn_id),
            res_id=res_id,
            item_id=str(item_info.item_id),
            task_type=chg_task.task_type,
        )

        asyncio.create_task(self.task_executor.execute_task(execute_input))


    # pending task에 있는 task들 할당 후 실행(Todo: 우선순위 적용 후 해당 type에 관련된 작업 중 하나만 빼와서 실행)
    async def _process_pending_tasks(self) -> None:
        pending_tasks = self._pending_tasks[:]
        self._pending_tasks.clear()

        for pending_task in pending_tasks:
            await self._allocate_and_execute(pending_task.task, pending_task.item_info)

    # 할당 후 실행하는 함수
    async def _allocate_and_execute(self, task: NextTaskResult, item_info: ItemStatusRecord) -> None:
        allocate_input = self._build_allocate_task_input(task, item_info)
        allocation_result = await self.task_allocator.allocate(allocate_input)

        if allocation_result.success and allocation_result.robot_id:
            if self.task_executor is None:
                logger.warning(
                    "task executor not configured; skipping execution for task_id=%s",
                    task.txn_id,
                )
                return
            await self.task_allocator.update_task_allocation(allocate_input, allocation_result.robot_id)
            execute_input = TaskExecutorInput(
                task_id=str(task.txn_id),
                res_id=allocation_result.robot_id,
                item_id=str(item_info.item_id),
                task_type=task.task_type,
            )
            asyncio.create_task(self.task_executor.execute_task(execute_input))
            return

        # 할당 실패 시 pending task에 append 후 나중에 할당 & 실행
        self._pending_tasks.append(_PendingTask(task=task, item_info=item_info))
        logger.info(
            "pending task queued: txn_id=%s item_id=%s reason=%s",
            task.txn_id,
            item_info.item_id,
            allocation_result.reason,
        )

    def _build_item_status_record(self, item_info: dict) -> ItemStatusRecord:
        last_task_type = item_info.get("last_task_type") or item_info.get("task_type")
        if isinstance(last_task_type, str):
            try:
                last_task_type = TaskType(last_task_type)
            except ValueError:
                last_task_type = None

        return ItemStatusRecord(
            item_id=item_info["item_id"],
            order_id=item_info.get("order_id") or item_info.get("ord_id") or 0,
            last_task_type=last_task_type,
            flow_stat=item_info.get("flow_stat"),
            is_defective=bool(item_info.get("is_defective", False)),
            ptn_id=item_info.get("ptn_id"),
        )

    def _build_allocate_task_input(self, task: NextTaskResult, item_info: ItemStatusRecord) -> AllocateTaskInput:
        return AllocateTaskInput(
            task_id=str(task.txn_id),
            item_id=item_info.item_id,
            req_res_type="",
            zone_nm=task.strg_loc,
            task_type=task.task_type,
        )
