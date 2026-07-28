"""Production-related Management gRPC methods."""

from __future__ import annotations

import grpc
import management_pb2  # type: ignore

from rpc.proto_helpers import (
    equip_task_to_proto,
    item_to_proto,
    item_pp_requirements_to_proto,
    order_item_progress_to_proto,
    priority_result_to_proto,
    production_order_to_proto,
    schedule_job_to_proto,
    stage_to_proto,
    equipment_to_proto,
)


class ProductionRpcMixin:
    """Production start and item read RPCs."""

    def StartProduction(self, request, context):
        """Unified StartProduction.
        
        Collects single ord_id and/or batch order_ids into a single list,
        then calls the Orchestrator's unified start_production method.
        """
        target_ids = []
        if request.ord_id and request.ord_id > 0:
            target_ids.append(request.ord_id)
        if request.order_ids:
            try:
                target_ids.extend([int(x) for x in request.order_ids if x.strip()])
            except ValueError:
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("Invalid order_ids format. Must be integers.")
                return management_pb2.StartProductionResponse()

        target_ids = list(set(target_ids)) # Deduplicate
        if not target_ids:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("Either ord_id or order_ids must be provided")
            return management_pb2.StartProductionResponse()

        orchestrator_thread = getattr(self, "orchestrator_thread", None)
        if orchestrator_thread is None:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("Orchestrator thread is not configured.")
            return management_pb2.StartProductionResponse()

        try:
            ack_model = orchestrator_thread.run_coro(
                self.orchestrator.start_production(target_ids)
            )
        except Exception as e:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return management_pb2.StartProductionResponse()

        # Map Pydantic model to Protobuf
        order_acks = []
        for o_ack in ack_model.orders:
            order_acks.append(
                management_pb2.StartProductionOrderAck(
                    ord_id=o_ack.ord_id,
                    accepted=o_ack.accepted,
                    reason=o_ack.reason or "",
                    item_id=o_ack.item_id or 0,
                    equip_task_txn_id=o_ack.equip_task_txn_id or 0,
                )
            )

        pb_ack = management_pb2.StartProductionAck(
            requested_count=ack_model.requested_count,
            accepted_count=ack_model.accepted_count,
            rejected_count=ack_model.rejected_count,
            orders=order_acks,
            message=ack_model.message or "",
        )

        # We omit legacy work_orders and result fields. PyQt will use ack fallback.
        return management_pb2.StartProductionResponse(ack=pb_ack)

    def StartShipping(self, request, context):
        """출하 트리거 — orchestrator.start_shipping(ord_id) 위임.

        호출 경로: 웹 /orders 화면 "출하 시작" 버튼 → FastAPI POST /api/production/shipping/start
                  → ManagementClient.start_shipping → 본 RPC.
        효과: 적재장 (Storage) 의 주문 item 들에 대해 TAT 출하 task (TASK_TYPE=PICK 등) 를
              생성/할당/실행. 자원 미가용 시 pending queue 에 적재되며 후속 RESOURCE_AVAILABLE
              이벤트로 재개된다. ord_stat 전이는 task 완료 이벤트 흐름이 담당하므로 본 RPC 는
              상태를 직접 바꾸지 않는다.
        """
        ord_id = int(getattr(request, "ord_id", 0) or 0)
        if ord_id <= 0:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("ord_id must be a positive integer")
            return management_pb2.StartShippingResponse()

        orchestrator_thread = getattr(self, "orchestrator_thread", None)
        if orchestrator_thread is None:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("Orchestrator thread is not configured.")
            return management_pb2.StartShippingResponse()

        try:
            item_ids = orchestrator_thread.run_coro(
                self.orchestrator.start_shipping(ord_id)
            )
        except Exception as exc:  # noqa: BLE001
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return management_pb2.StartShippingResponse()

        item_ids = [int(x) for x in (item_ids or [])]
        accepted = True
        if item_ids:
            message = f"Shipping task scheduled: {len(item_ids)} item(s)."
        else:
            # 빈 결과 = 적재 위치가 없어 ship task 가 생성되지 않은 경우.
            # 운영 측면에서는 reject 이 아니라 noop 으로 분류해 UI 가 토스트만 띄우면 된다.
            message = (
                "No items eligible for shipping. Verify storage allocation and "
                "production completion in PyQt before retrying."
            )
        return management_pb2.StartShippingResponse(
            ord_id=ord_id,
            item_ids=item_ids,
            accepted=accepted,
            message=message,
        )

    def ListItems(self, request, context):
        stage_filter = (
            management_pb2.ItemStage.Name(request.stage_filter).replace("ITEM_STAGE_", "")
            if request.stage_filter
            else None
        )
        items = self.item_query_service.list_items(
            order_id=request.order_id or None,
            stage=stage_filter if stage_filter and stage_filter != "UNSPECIFIED" else None,
            limit=request.limit or 100,
        )
        return management_pb2.ListItemsResponse(items=[item_to_proto(it) for it in items])

    def ListProductionOrders(self, request, context):
        rows = self.production_order_query_service.list_orders(
            status_filters=list(request.status_filters),
            limit=request.limit or 200,
        )
        return management_pb2.ListProductionOrdersResponse(
            orders=[production_order_to_proto(row) for row in rows]
        )

    def ListEquipTasks(self, request, context):
        rows = self.item_query_service.list_equip_tasks(
            res_id=request.res_id or None,
            item_id=request.item_id or None,
            limit=request.limit or 100,
        )
        return management_pb2.ListEquipTasksResponse(tasks=[equip_task_to_proto(row) for row in rows])

    def GetItemPpRequirements(self, request, context):
        row = self.item_query_service.get_item_pp_requirements(request.item_id)
        if row is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"item_id={request.item_id} not found")
            return management_pb2.ItemPpRequirementsResponse()
        return item_pp_requirements_to_proto(row)

    def ListEquipment(self, request, context):
        rows = self.item_query_service.list_equipment()
        return management_pb2.ListEquipmentResponse(
            equipment=[equipment_to_proto(row) for row in rows]
        )

    def ListStages(self, request, context):
        rows = self.item_query_service.list_stages()
        return management_pb2.ListStagesResponse(stages=[stage_to_proto(row) for row in rows])

    def ListOrderItemProgress(self, request, context):
        rows = self.item_query_service.list_order_item_progress()
        return management_pb2.ListOrderItemProgressResponse(
            items=[order_item_progress_to_proto(row) for row in rows]
        )

    def ListScheduleJobs(self, request, context):
        rows = self.schedule_query_service.list_jobs()
        return management_pb2.ListScheduleJobsResponse(
            jobs=[schedule_job_to_proto(row) for row in rows]
        )

    def CalculateSchedulePriority(self, request, context):
        try:
            rows = self.schedule_query_service.calculate_priority(list(request.order_ids))
        except ValueError as exc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(exc))
            return management_pb2.CalculateSchedulePriorityResponse()
        except LookupError as exc:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return management_pb2.CalculateSchedulePriorityResponse()
        return management_pb2.CalculateSchedulePriorityResponse(
            results=[priority_result_to_proto(row) for row in rows]
        )
