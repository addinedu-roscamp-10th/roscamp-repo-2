"""Event-driven orchestrator for Management production start flows."""

from __future__ import annotations

import asyncio
import logging
import heapq
from itertools import count
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from services.contracts.models import (
    StartProductionBatchAckModel,
    StartProductionOrderAckModel,
    ItemStatusRecord,
    NextTaskResult,
    ShipTaskResult,
    ScheduleNextTaskInput,
    ExecuteTaskInput,
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


_PrioritizedPendingTask = tuple[int, int, _PendingTask]

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
        self.event_bridge.subscribe(
            EventType.ARM_RETURN_COMPLETED,
            self.on_arm_return_completed,
            "orchestrator.arm_return_completed",
        )
        self._pending_tasks: dict[str, list[_PrioritizedPendingTask]] = defaultdict(list)
        self._pending_task_seq = count()  # 같은 priority 작업의 FIFO 순서 보장용

    async def start_production(self, ord_ids: list[int]) -> "StartProductionBatchAckModel":
        """생산 시작 진입점."""
        logger.info("start production requested: ord_ids=%s", ord_ids)
        results: list[StartProductionOrderAckModel] = []
        accepted = 0
        rejected = 0
        for ord_id in ord_ids:
            try:
                # 주문 수량 확인
                target_qty = await self.state_manager.get_order_target_qty(ord_id)
                
                # 주문 수량 조회 실패
                if target_qty is None:
                    result = self._build_rejected_ack(
                        ord_id,
                        "Order target quantity not found.",
                    )
                    logger.warning(
                        "start production rejected: ord_id=%s reason=%s",
                        ord_id,
                        result.reason,
                    )
                    rejected += 1
                    results.append(result)
                    continue

                self.task_manager.log_slot_table()
                # TM이 빈 슬롯 찾기
                start_pos = await self.state_manager.get_empty_start_slot(target_qty)

                if start_pos is None:
                    result = self._build_rejected_ack(
                        ord_id,
                        f"Not enough rack slots. target_qty={target_qty}",
                    )
                    logger.warning(
                        "start production rejected: ord_id=%s reason=%s",
                        ord_id,
                        result.reason,
                    )
                    rejected += 1
                    results.append(result)
                    continue

                # 주문 시작 위치 저장
                self.state_manager.orders.setdefault(ord_id, {})["rack_start_pos"] = start_pos

                # 슬롯 예약
                self.task_manager.reserve_rack_slots(
                    order_id=ord_id,
                    start_pos=start_pos,
                    target_qty=target_qty,
                )

                # 로그 출력 추천
                #self.task_manager.log_slot_table()

                # SM item 생성
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
                logger.warning(
                    "start production rejected: ord_id=%s reason=%s",
                    ord_id,
                    result.reason or "unknown",
                )

            results.append(result)

        batch_result = self._build_batch_ack(ord_ids, results, accepted, rejected)
        rejected_orders = [
            {"ord_id": order_result.ord_id, "reason": order_result.reason or "unknown"}
            for order_result in results
            if not order_result.accepted
        ]
        logger.info(
            "start production completed: requested=%d accepted=%d rejected=%d",
            batch_result.requested_count,
            batch_result.accepted_count,
            batch_result.rejected_count,
        )
        if rejected_orders:
            logger.warning("start production rejected orders: %s", rejected_orders)
        return batch_result

    async def start_shipping(self, ord_id: int | None = None) -> list[int]:
        """출고 시작 진입점."""
        if ord_id is None:
            logger.warning("start ship failed: ord_id is required")
            return []

        return await self._schedule_ship_task(ord_id, event="ship_start")

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

        # 출고는 따로
        if event.payload.get("task_type") == TaskType.PICK:
            item_info = await self.state_manager.get_item(item_id)

            # 현재 완료된 batch pop
            finished_batch = self.task_manager.pop_finished_ship_batch(
                item_info.order_id
            )

            # batch 전체 상태 업데이트
            if finished_batch:
                await self.state_manager.complete_ship_batch(
                    finished_batch
                )
            # 다음 batch 출고 계속
            if self.task_manager.has_ship_plan(item_info.order_id):
                await self._schedule_ship_task(item_info.order_id, event="pick_done")
            return

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

        # 출고는 따로
        if subtask_type == "toship_src_arrived":
            item_info = await self.state_manager.get_item(item_id)
            await self._schedule_ship_task(
                item_info.order_id,
                event="toship_src_arrived",
            )
            return

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
        # 작업에 할당되지 않은 가용 가능한 AMR은 충전소로 이동
        if req_res_type == "TAT" and event.res_id is not None:
            await self._return_idle_amr_to_charger_if_needed(
                event.res_id,
                source=str(event.payload.get("task_id") or "resource_available"),
            )

    async def on_amr_bat_low(self, event: Event) -> None:
        """배터리가 부족한 AMR의 충전 task를 생성하고 실행한다."""
        item_id = event.item_id
        if item_id is None:
            raise ValueError("AMR_BATTERY_LOW requires item_id")
        amr_id = event.payload.get("amr_id")
        if amr_id is None:
            raise ValueError("AMR_BATTERY_LOW requires amr_id")
        
        arm_id = event.payload.get("arm_id")
        if arm_id is None:
            raise ValueError("AMR_BATTERY_LOW requires arm_id")

        logger.warning("battery low event received: amr_id=%s arm_id=%s item_id=%s", amr_id, arm_id, item_id)
        
        #executor 작업 중지 -> 로봇팔은 상차하려던거 멈추고 어디 내려둠 + 베이스 위치로
        await self.task_executor.handle_emergency_return(item_id, amr_id , arm_id)

    async def on_arm_return_completed(self, event: Event) -> None:
            """로봇팔이 아이템 복구를 완료했을 때 호출되는 콜백 (이벤트 구독에 의해 실행)"""
            item_id = event.item_id
            amr_id = event.res_id
            if amr_id is None:
                raise ValueError("ARM_RETURN_COMPLETED requires res_id")

            logger.info("[ OK ] arm_return_completed 이벤트 : 복구 완료 확인.")

            await self._schedule_next_task( #TOPP 생성
                ScheduleNextTaskInput(
                    item_id=item_id,
                    planning_event="amr_battery_low_ToPP", 
                    req_res_id=None,
                )
            )

            await self._schedule_next_task( #TOCHG 생성
                ScheduleNextTaskInput(
                    item_id=item_id,
                    planning_event="amr_battery_low_ToCHG", 
                    req_res_id=amr_id,
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

        # 
        for next_task in next_tasks:
            task_item_info = item_info
            if next_task.item_id != item_info.item_id:  # PA_DP의 경우 불량과 보충 task의 item info가 다름
                task_item_info = await self.state_manager.get_item(next_task.item_id)

            await self._allocate_and_execute(
                next_task,
                task_item_info,
                req_res_id=input_data.req_res_id,
            )

    async def _schedule_ship_task(
        self,
        ord_id: int,
        event: str,
    ) -> list[int]:
        """출고 이벤트에 따라 다음 출고 task를 만들고 실행한다."""
        item_locations = await self._collect_ship_item_locations(ord_id)
        if not item_locations:
            logger.warning(
                "start ship failed: ord_id=%s reason=no eligible item_locations event=%s",
                ord_id,
                event,
            )
            return []
        ship_task = await self.task_manager.create_ship_task(
            order_id=ord_id,
            item_locations=item_locations,
            event=event,
        )
        return await self._execute_ship_task(ord_id, ship_task, item_locations)

    async def _process_pending_bucket(self, req_res_type: str) -> None:
        """해당 자원 타입의 대기 작업들을 처리한다."""
        bucket = self._pending_tasks.pop(req_res_type, None)  # 해당 자원에 해당하는 pending queue pop
        if not bucket:
            return

        pending_tasks: list[_PendingTask] = []  # 임시 작업 큐
        while bucket:
            _, _, pending_task = heapq.heappop(bucket)  # queue에서 우선순위대로 꺼내서
            pending_tasks.append(pending_task)  # pending tasks에 넣고

        for pending_task in pending_tasks:  # pending task를 하나씩 할당 -> 실행
            await self._allocate_and_execute(
                pending_task.task,
                pending_task.item_info,
            )

    async def _allocate_and_execute(
        self,
        task: NextTaskResult,
        item_info: ItemStatusRecord,
        req_res_id: str | None = None,
    ) -> None:
        """자원를 할당하고 실행한다."""
        allocate_input = self._build_allocate_task_input(task, item_info, req_res_id,)
        allocation_result = await self.task_allocator.allocate(allocate_input)

        if allocation_result.success and allocation_result.res_id:
            if self.task_executor is None:
                logger.warning(
                    "task executor not configured; skipping execution for task_id=%s",
                    task.txn_id,
                )
                return
            execute_input = self._build_execute_task_input(
                task,
                item_info,
                allocation_result.res_id,
            )
            asyncio.create_task(self.task_executor.execute_task(execute_input))
            return

        # 할당 실패 시 pending task에 append 후 나중에 할당 -> 실행
        req_res_type = allocation_result.req_res_type
        self._push_pending_task(
            req_res_type,
            _PendingTask(
                task=task,
                item_info=item_info,
            ),
        )
        logger.info(
            "pending task queued: txn_id=%s item_id=%s req_res_type=%s reason=%s",
            task.txn_id,
            item_info.item_id,
            req_res_type,
            allocation_result.reason,
        )

    async def _execute_ship_task(
        self,
        ord_id: int,
        ship_task: ShipTaskResult | None,
        item_locations: list[tuple[int, int, int]],
    ) -> list[int]:
        """ShipTaskResult를 일반 task 실행 경로로 연결한다."""
        if ship_task is None:
            logger.info("no shipping task created: ord_id=%s item_count=%s", ord_id, len(item_locations))
            return []

        ship_batch = ship_task.batch or item_locations[:1]
        if not ship_batch:
            logger.info("shipping task has no item batch: ord_id=%s task_id=%s", ord_id, ship_task.txn_id)
            return []

        item_id, strg_row, strg_col = ship_batch[0]
        item_info = await self.state_manager.get_item(item_id)
        item_info.strg_loc = (strg_row, strg_col)

        await self._allocate_and_execute(
            NextTaskResult(
                item_id=item_id,
                txn_id=ship_task.txn_id,
                task_type=ship_task.task_type,
                priority=ship_task.priority,
                batch=ship_batch,
            ),
            item_info,
        )
        return [item_id]
    def _push_pending_task(self, req_res_type: str, pending_task: _PendingTask) -> None:
        """task를 pending bucket에 추가한다."""
        heapq.heappush(
            self._pending_tasks[req_res_type],
            (-pending_task.task.priority, next(self._pending_task_seq), pending_task),
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
            task_type=task.task_type,
        )

    def _build_execute_task_input(
        self,
        task: NextTaskResult,
        item_info: ItemStatusRecord,
        res_id: str,
    ) -> ExecuteTaskInput:
        """executor 입력을 생성한다."""
        return ExecuteTaskInput(
            task_id=str(task.txn_id),
            res_id=res_id,
            item_id=str(item_info.item_id),
            task_type=task.task_type,
            payload=self._build_execute_payload(task, item_info),
        )

    async def _return_idle_amr_to_charger_if_needed(self, res_id: str, source: str) -> None:
        """대기 중인 TAT가 할당되지 않으면 충전소로 이동한다."""
        if not self.state_manager.is_res_available(res_id):
            return
        await self.task_executor.return_amr_to_charger(res_id, source=source)

    def _build_execute_payload(
        self,
        task: NextTaskResult,
        item_info: ItemStatusRecord,
    ) -> dict[str, Any]:
        """어댑터가 필요로 하는 정보들을 넣어준다."""
        payload: dict[str, Any] = {"item_id": item_info.item_id}

        if item_info.ptn_id is not None:
            payload["ptn_loc_id"] = item_info.ptn_id

        if item_info.strg_loc:
            payload["strg_loc"] = item_info.strg_loc

        # 출고 때는 여러 item을 한번에 작업하기 때문에 batch 사용
        if task.batch:
            payload["batch"] = task.batch

        # INSP task 는 AIAdapter 가 image_path/captured_at/label 을 요구한다.
        # INSP_IMAGE_UPLOADED 발행 시 state_manager.update_inspection_image 로
        # 저장된 정보를 1회 소비 후 payload 에 병합한다.
        if task.task_type == TaskType.INSP:
            consume = getattr(self.state_manager, "consume_inspection_image", None)
            if consume is not None:
                image_info = consume(item_info.item_id)
                if image_info:
                    for key in ("image_path", "image_url", "captured_at", "label", "stage", "camera_id"):
                        value = image_info.get(key)
                        if value is not None and key not in payload:
                            payload[key] = value
                    logger.info(
                        "INSP payload built: item_id=%s image_path=%s",
                        item_info.item_id,
                        payload.get("image_path"),
                    )
                else:
                    logger.warning(
                        "INSP payload missing image info: item_id=%s — AIAdapter will fail",
                        item_info.item_id,
                    )

        return payload

    async def _collect_ship_item_locations(self, ord_id: int) -> list[tuple[int, int, int]]:
        """주문별 출고 대상 item의 적재 위치를 DB에서 직접 조회."""
        return await self.state_manager.load_ship_item_locations(ord_id)


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
