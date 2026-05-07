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
    ScheduleNextTaskInput,
    ExecuteTaskInput,
    AllocateTaskInput,
    Event,
)
from services.contracts.enums import (
    EventType,
    TxnStat,
    ResourceBindingPolicy,
    get_resource_binding_policy,
)
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
    def __init__(
        self,
        task_manager: ITaskManager,
        task_allocator: ITaskAllocator,
        state_manager: IStateManager,
        event_bridge: IEventBridge,
        task_executor: ITaskExecutor,
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
        """생산 시작 진입점."""
        logger.info("start production requested: ord_ids=%s", ord_ids)
        results: list[StartProductionOrderAckModel] = []
        accepted = 0
        rejected = 0

        for ord_id in ord_ids:
            try:
                result = await self.state_manager.start_production(ord_id)
            except Exception as exc:
                logger.warning("start production failed: ord_id=%s reason=%s", ord_id, exc)
                result = self._build_rejected_ack(ord_id, str(exc))

            if result.accepted:
                accepted += 1
                for item_id in result.item_ids:
                    await self._schedule_next_task(
                        ScheduleNextTaskInput(item_id=item_id)
                    )
            else:
                rejected += 1

            results.append(result)

        batch_result = self._build_batch_ack(ord_ids, results, accepted, rejected)
        logger.info(
            "start production completed: requested=%d accepted=%d",
            batch_result.requested_count,
            batch_result.accepted_count,
        )
        return batch_result

    async def start_shipping(self, ord_ids: int | None = None) -> list[int]:  # 출고 구현할 때 필요
        """출고 시작 진입점."""
        return []

    async def on_task_completed(self, event: Event) -> None:
        """작업 완료 후 다음 작업을 생성, 할당, 실행한다."""
        task_status = event.payload.get("status")
        if task_status == TxnStat.FAIL.value:
            logger.info(
                "task completed event reported failure: task_id=%s item_id=%s task_type=%s",
                event.payload.get("task_id"),
                event.item_id,
                event.payload.get("task_type"),
            )
            return

        item_id = event.item_id
        if item_id is None:
            raise ValueError("TASK_COMPLETED requires item_id")

        await self._schedule_next_task(
            ScheduleNextTaskInput(
                item_id=item_id,
                last_task_type=event.payload.get("task_type"),
            )
        )

    async def on_subtask_completed(self, event: Event) -> None:
        """세부 작업 완료 이벤트를 처리한다."""
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
        item_id = event.item_id
        if item_id is None:
            raise ValueError("SUBTASK_COMPLETED requires item_id")

        await self._schedule_next_task(
            ScheduleNextTaskInput(
                item_id=item_id,
                planning_event=subtask_type,
                last_task_type=event.payload.get("task_type"),
            )
        )

    async def on_amr_charged(self, event: Event) -> None:
        """AMR 충전 완료 시 대기 중인 TAT 작업을 재시도한다."""
        res_id = event.res_id
        logger.info("amr charged: res_id=%s req_res_type=%s", res_id, "TAT")
        await self._process_pending_bucket("TAT")

    async def on_resource_available(self, event: Event) -> None:
        """가용가능한 자원의 대기 작업을 재시도한다."""
        req_res_type = event.payload.get("req_res_type")
        logger.info(
            "resource available: res_id=%s req_res_type=%s",
            event.res_id,
            req_res_type,
        )
        await self._process_pending_bucket(req_res_type)

    async def on_amr_bat_low(self, event: Event) -> None:
        """배터리가 부족한 AMR의 충전 task를 생성하고 실행한다."""
        item_id = event.item_id
        if item_id is None:
            raise ValueError("AMR_BATTERY_LOW requires item_id")
        res_id = event.res_id
        if res_id is None:
            raise ValueError("AMR_BATTERY_LOW requires res_id")

        logger.warning("battery low event received: res_id=%s item_id=%s", res_id, item_id)
        await self._schedule_next_task(
            ScheduleNextTaskInput(
                item_id=item_id,
                planning_event="amr_battery_low",
                req_res_id=res_id,
            )
        )

    async def _schedule_next_task(
        self,
        input_data: ScheduleNextTaskInput,
    ) -> None:
        """다음 작업을 생성하고 자원을 할당, 실행한다."""
        item_info = await self.state_manager.get_item(input_data.item_id)
        item_info.last_task_type = input_data.last_task_type

        next_tasks = await self.task_manager.create_next_task(
            item_info,
            input_data.planning_event,
        )

        if not next_tasks:
            logger.info("no next task planned: item_id=%s", input_data.item_id)
            return

        for next_task in next_tasks:
            await self._allocate_and_execute(
                next_task,
                item_info,
                req_res_id=input_data.req_res_id,
            )

    async def _process_pending_bucket(self, req_res_type: str) -> None:
        """해당 자원 타입의 대기 작업들을 처리한다."""
        bucket = self._pending_tasks.get(req_res_type)
        if not bucket:
            return

        for _ in range(len(bucket)):
            pending_task = bucket.popleft()
            await self._allocate_and_execute(
                pending_task.task,
                pending_task.item_info,
            )

        if not bucket: # 빈 bucket 정리
            self._pending_tasks.pop(req_res_type, None)

    async def _allocate_and_execute(
        self,
        task: NextTaskResult,
        item_info: ItemStatusRecord,
        req_res_id: str | None = None,
    ) -> None:
        """자원를 할당하고 실행한다."""
        allocate_input = self._build_allocate_task_input(task, item_info, req_res_id,)
        allocation_result = await self.task_allocator.allocate(allocate_input)

        if allocation_result.success and allocation_result.robot_id:
            if self.task_executor is None:
                logger.warning(
                    "task executor not configured; skipping execution for task_id=%s",
                    task.txn_id,
                )
                return
            execute_input = self._build_execute_task_input(
                task,
                item_info,
                allocation_result.robot_id,
            )
            asyncio.create_task(self.task_executor.execute_task(execute_input))
            return

        # 할당 실패 시 pending task에 append 후 나중에 할당 -> 실행
        req_res_type = allocation_result.req_res_type
        self._pending_tasks[req_res_type].append(
            _PendingTask(
                task=task,
                item_info=item_info,
            )
        )
        logger.info(
            "pending task queued: txn_id=%s item_id=%s req_res_type=%s reason=%s",
            task.txn_id,
            item_info.item_id,
            req_res_type,
            allocation_result.reason,
        )

    def _resolve_req_res_id(
        self,
        task: NextTaskResult,
        item_info: ItemStatusRecord,
        req_res_id: str | None = None,
    ) -> str | None:
        """작업 할당에 사용할 req_res_id를 결정한다."""
        if req_res_id is not None:
            return req_res_id
        if get_resource_binding_policy(task.task_type) == ResourceBindingPolicy.REQUIRED:
            return item_info.req_res_id
        return None

    def _build_allocate_task_input(
        self,
        task: NextTaskResult,
        item_info: ItemStatusRecord,
        req_res_id: str | None = None,
    ) -> AllocateTaskInput:
        """allocator 입력을 생성한다."""
        return AllocateTaskInput(
            task_id=str(task.txn_id),
            item_id=item_info.item_id,
            req_res_id=self._resolve_req_res_id(
                task,
                item_info,
                req_res_id=req_res_id,
            ),
            zone_nm=task.strg_loc,
            task_type=task.task_type,
        )

    def _build_execute_task_input(
        self,
        task: NextTaskResult,
        item_info: ItemStatusRecord,
        robot_id: str,
    ) -> ExecuteTaskInput:
        """executor 입력을 생성한다."""
        return ExecuteTaskInput(
            task_id=str(task.txn_id),
            res_id=robot_id,
            item_id=str(item_info.item_id),
            task_type=task.task_type,
        )

    def _build_rejected_ack(self, ord_id: int, reason: str) -> "StartProductionOrderAckModel":
        """start_production 예외 발생 시 rejected ack를 생성한다."""
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
