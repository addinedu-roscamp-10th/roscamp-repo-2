"""Event-driven orchestrator for Management production start flows."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.contracts.models import (
    StartProductionBatchAckModel,
    StartProductionOrderAckModel,
    ItemStatusRecord,
    NextTaskResult,
    TaskExecutorInput,
    AllocateTaskInput,
    Event,
)
from services.contracts.enums import (
    EventType,
    TaskType,
    TxnStat,
    ResourceBindingPolicy,
    get_resource_binding_policy,
)
from services.contracts.protocols import IOrchestrator
from services.core.task_allocator import get_required_resource_type


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
            EventType.RESOURCE_AVAILABLE,
            self.on_resource_available,
            "orchestrator.resource_available",
        )
        self.event_bridge.subscribe(
            EventType.AMR_CHARGED,
            self.on_amr_charged,
            "orchestrator.amr_charged",
        )
        self.event_bridge.subscribe(
            EventType.AMR_BATTERY_LOW,
            self.on_amr_bat_low,
            "orchestrator.amr_battery_low",
        )

        self._pending_tasks: dict[str, deque[_PendingTask]] = defaultdict(deque)

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
                    await self._schedule_next_task(
                        Event(
                            event_type=EventType.ITEM_STATUS_CHANGED,
                            item_id=item_id,
                        )
                    )
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

    # 작업 생성 -> 작업 할당 -> 작업 실행 
    async def _schedule_next_task(
        self,
        event: Event,
        planning_event: str | None = None,
    ) -> None:
        if event.item_id is None:
            logger.info("cannot schedule next task without item_id: %s", event)
            return

        item_info = await self.state_manager.get_item(event.item_id)

        completed_task_type = event.payload.get("task_type")
        if isinstance(completed_task_type, str):
            try:
                item_info.last_task_type = TaskType(completed_task_type)
            except ValueError:
                pass

        next_tasks = await self.task_manager.create_next_task(item_info, planning_event)

        if not next_tasks:
            logger.info("no next task planned: item_id=%s", event.item_id)
            return

        for next_task in next_tasks:
            await self._allocate_and_execute(next_task, item_info)

    # task가 끝났을 때는 다음 task 생성, 할당, 실행
    async def on_task_completed(self, event: Event) -> None:
        """TASK_COMPLETED event handler."""
        task_status = event.payload.get("status")
        if task_status == TxnStat.FAIL.value:
            logger.info(
                "task completed event reported failure: task_id=%s item_id=%s task_type=%s",
                event.payload.get("task_id"),
                event.item_id,
                event.payload.get("task_type"),
            )
            return

        await self._schedule_next_task(event)

    # 세부 공정 완료 시 작업
    async def on_subtask_completed(self, event: Event) -> None:
        """세부 공정 완료 이벤트 처리."""
        subtask_type = event.payload.get("subtask_type")
        if subtask_type is None:
            logger.info("subtask completed event without subtask_type payload: %s", event)
            return
        logger.info(
            "subtask completed: task_id=%s item_id=%s subtask_type=%s",
            event.payload.get("task_id"),
            event.item_id,
            subtask_type,
        )
        await self._schedule_next_task(event, subtask_type)

    # TAT 배터리가 충전되었을 때, TAT pending task들 할당 시도
    async def on_amr_charged(self, event: Event) -> None:
        """AMR_CHARGED event handler."""
        res_id = getattr(event, "res_id", None)
        logger.info("amr charged: res_id=%s req_res_type=%s", res_id, "TAT")
        await self._process_pending_bucket("TAT")

    async def on_resource_available(self, event: Event) -> None:
        req_res_type = event.payload.get("req_res_type")
        if not isinstance(req_res_type, str) or not req_res_type:
            logger.debug("resource available event without req_res_type: %s", event)
            return
        logger.info(
            "resource available: res_id=%s req_res_type=%s",
            getattr(event, "res_id", None),
            req_res_type,
        )
        await self._process_pending_bucket(req_res_type)

    # item 상태 변경 시에 ui 변경 등(monitoring manager?)
    async def on_item_status_changed(self, event: Event) -> None:
        """ITEM_STATUS_CHANGED event handler."""
        logger.debug("item status changed: item_id=%s", getattr(event, "item_id", None))

    # State manager는 battery low를 감지했을 때, 해당 로봇에 대한 정보(어떤 item_id를 들고 있는지)를 조회한 후에 event에 담아서 보내야함.
    # 근데 우리는 wait_ld에서만 bat low가 뜨는 것으로 가정하기 때문에 아마 항상 없을 것
    async def on_amr_bat_low(self, event: Event) -> None:
        item_id = getattr(event, "item_id", None)
        if item_id is None: # 이미 charging zone으로 가고 있을 때는 item id가 없음
            logger.warning("battery low event without item_id: %s", event)
            return

        res_id = getattr(event, "res_id", None)
        if res_id is None:
            logger.warning("battery low event without res_id: %s", event)
            return

        logger.warning("battery low event received: res_id=%s item_id=%s", res_id, item_id)
        item_info = await self.state_manager.get_item(item_id)
        
        
        chg_tasks = await self.task_manager.create_next_task(item_info, "amr_battery_low")
        chg_task = chg_tasks[0]
        allocate_input = AllocateTaskInput(
            task_id=str(chg_task.txn_id),
            item_id=item_info.item_id,
            req_res_type=get_required_resource_type(chg_task.task_type, chg_task.strg_loc),
            req_res_id=res_id,
            zone_nm=chg_task.strg_loc,
            task_type=chg_task.task_type,
        )
        allocation_result = await self.task_allocator.allocate(allocate_input)
        if not allocation_result.success or not allocation_result.robot_id:
            logger.warning(
                "failed to allocate forced charging task: task_id=%s item_id=%s res_id=%s reason=%s",
                chg_task.txn_id,
                item_info.item_id,
                res_id,
                allocation_result.reason,
            )
            return
        execute_input = TaskExecutorInput(
            task_id=str(chg_task.txn_id),
            res_id=allocation_result.robot_id,
            item_id=str(item_info.item_id),
            task_type=chg_task.task_type,
        )

        asyncio.create_task(self.task_executor.execute_task(execute_input))

    async def _process_pending_bucket(self, req_res_type: str) -> None:
        bucket = self._pending_tasks.get(req_res_type)
        if not bucket:
            return

        pending_count = len(bucket)
        for _ in range(pending_count):
            pending_task = bucket.popleft()
            await self._allocate_and_execute(pending_task.task, pending_task.item_info)

        if not bucket:
            self._pending_tasks.pop(req_res_type, None)

    # 할당 후 실행하는 함수
    async def _allocate_and_execute(self, task: NextTaskResult, item_info: ItemStatusRecord) -> None:
        allocate_input = AllocateTaskInput(
            task_id=str(task.txn_id),
            item_id=item_info.item_id,
            req_res_type=get_required_resource_type(task.task_type, task.strg_loc),
            req_res_id=(
                item_info.req_res_id
                if get_resource_binding_policy(task.task_type) == ResourceBindingPolicy.REQUIRED
                else None
            ),
            zone_nm=task.strg_loc,
            task_type=task.task_type,
        )
        allocation_result = await self.task_allocator.allocate(allocate_input)

        if allocation_result.success and allocation_result.robot_id:
            if self.task_executor is None:
                logger.warning(
                    "task executor not configured; skipping execution for task_id=%s",
                    task.txn_id,
                )
                return
            execute_input = TaskExecutorInput(
                task_id=str(task.txn_id),
                res_id=allocation_result.robot_id,
                item_id=str(item_info.item_id),
                task_type=task.task_type,
            )
            asyncio.create_task(self.task_executor.execute_task(execute_input))
            return

        # 할당 실패 시 pending task에 append 후 나중에 할당 & 실행
        self._pending_tasks[allocate_input.req_res_type].append(
            _PendingTask(task=task, item_info=item_info)
        )
        logger.info(
            "pending task queued: txn_id=%s item_id=%s req_res_type=%s reason=%s",
            task.txn_id,
            item_info.item_id,
            allocate_input.req_res_type,
            allocation_result.reason,
        )


    # 아래 함수는 rpc 요청에 대한 응답의 포맷을 맞춰주기 위해서 사용
    def _build_rejected_ack(self, ord_id: int, reason: str) -> "StartProductionOrderAckModel":
        """state manager의 정상 reject가 아닌 exception fallback"""
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
        """여러 주문들의 start_production 결과를 모아서 반환."""
        from ..contracts.models import StartProductionBatchAckModel

        return StartProductionBatchAckModel(
            requested_count=len(ord_ids),
            accepted_count=accepted,
            rejected_count=rejected,
            orders=results,
            message="DB-backed start_production completed." if accepted else "No orders were accepted.",
        )
