"""Helpers for converting internal records to Management protobuf messages."""

from __future__ import annotations

import management_pb2  # type: ignore


_STAGE_NAME_TO_ENUM = {
    "QUE": 1,
    "MM": 2,
    "DM": 3,
    "TR_PP": 4,
    "PP": 5,
    "IP": 6,
    "TR_LD": 7,
    "SH": 8,
}

_FLOW_TO_LEGACY_STAGE = {
    "CREATED": "QUE",
    "CAST": "MM",
    "WAIT_PP": "TR_PP",
    "PP": "PP",
    "WAIT_INSP": "QUE",
    "INSP": "IP",
    "WAIT_PA": "QUE",
    "PA": "PP",
    "STORED": "TR_LD",
    "PICK": "TR_LD",
    "READY_TO_SHIP": "SH",
    "DISCARDED": "SH",
    "HOLD": "QUE",
}

_WO_STATUS_TO_ENUM = {"QUE": 1, "PROC": 2, "SUCC": 3, "FAIL": 4}


def _ts(iso_str):
    return management_pb2.Timestamp(iso8601=iso_str or "")


def _legacy_stage(item) -> str:
    flow_stat = getattr(item, "flow_stat", None)
    if flow_stat is None:
        cur_stat = getattr(item, "cur_stat", None)
        if cur_stat:
            return str(cur_stat)
    return _FLOW_TO_LEGACY_STAGE.get((flow_stat or "").upper(), "QUE")


def _current_resource(item) -> str:
    zone_nm = getattr(item, "zone_nm", None)
    if zone_nm is None:
        cur_res = getattr(item, "cur_res", None)
        if cur_res:
            return str(cur_res)
    return zone_nm or ""


def item_to_proto(item):
    """smartcast Item -> proto Item (SPEC-C3, 2026-04-20)."""
    updated = item.updated_at.isoformat() if getattr(item, "updated_at", None) else ""
    stage = _legacy_stage(item)
    item_id = getattr(item, "item_stat_id", None)
    if item_id is None:
        item_id = getattr(item, "item_id", 0)
    kwargs = {
        "id": item_id,
        "order_id": str(getattr(item, "ord_id", "") or ""),
        "cur_stage": _STAGE_NAME_TO_ENUM.get(stage, 0),
        "curr_res": _current_resource(item),
        "insp_id": 0,
        "mfg_at": _ts(updated),
        "flow_stat": str(getattr(item, "flow_stat", "") or ""),
        "zone_nm": str(getattr(item, "zone_nm", "") or ""),
        "cur_stat": stage,
    }
    result = getattr(item, "result", None)
    if result is not None:
        kwargs["result"] = result
    is_defective = getattr(item, "is_defective", None)
    if is_defective is not None:
        kwargs["is_defective"] = is_defective
    return management_pb2.Item(**kwargs)


def production_order_to_proto(row):
    return management_pb2.ProductionOrder(
        ord_id=row.ord_id,
        user_id=row.user_id,
        latest_stat=row.latest_stat or "",
        company_name=row.company_name or "",
        customer_name=row.customer_name or "",
        total_amount=row.total_amount or 0.0,
        requested_delivery=row.requested_delivery or "",
        confirmed_delivery=row.confirmed_delivery or "",
        created_at=row.created_at or "",
    )


def pattern_to_proto(row):
    kwargs = {"ord_id": row.ord_id}
    if row.pattern_id is not None:
        kwargs["pattern_id"] = row.pattern_id
    if row.ptn_loc_id is not None:
        kwargs["ptn_loc_id"] = row.ptn_loc_id
    return management_pb2.PatternAssignment(**kwargs)


def equip_task_to_proto(row):
    kwargs = {
        "txn_id": row.txn_id,
        "res_id": row.res_id or "",
        "task_type": row.task_type or "",
        "txn_stat": row.txn_stat or "",
        "req_at": row.req_at.isoformat() if row.req_at else "",
        "start_at": row.start_at.isoformat() if row.start_at else "",
        "end_at": row.end_at.isoformat() if row.end_at else "",
    }
    if row.item_id is not None:
        kwargs["item_id"] = row.item_id
    if row.strg_loc_id is not None:
        kwargs["strg_loc_id"] = row.strg_loc_id
    if row.ship_loc_id is not None:
        kwargs["ship_loc_id"] = row.ship_loc_id
    return management_pb2.EquipTask(**kwargs)


def item_pp_requirements_to_proto(row):
    return management_pb2.ItemPpRequirementsResponse(
        item_id=row.item_id,
        ord_id=row.ord_id,
        pp_options=[
            management_pb2.PpOptionDefinition(
                pp_id=opt.pp_id,
                pp_nm=opt.pp_nm or "",
                extra_cost=opt.extra_cost or 0.0,
            )
            for opt in row.pp_options
        ],
        pp_task_status=[
            management_pb2.PpTaskStatus(**{
                **{
                    "txn_id": txn.txn_id,
                    "ord_id": txn.ord_id,
                    "pp_nm": txn.pp_nm or "",
                    "txn_stat": txn.txn_stat or "",
                    "req_at": txn.req_at.isoformat() if txn.req_at else "",
                    "start_at": txn.start_at.isoformat() if txn.start_at else "",
                    "end_at": txn.end_at.isoformat() if txn.end_at else "",
                },
                **({"map_id": txn.map_id} if txn.map_id is not None else {}),
                **({"item_id": txn.item_id} if txn.item_id is not None else {}),
                **({"operator_id": txn.operator_id} if txn.operator_id is not None else {}),
            })
            for txn in row.pp_task_status
        ],
    )


def equipment_to_proto(row):
    kwargs = {
        "res_id": row.res_id,
        "res_type": row.res_type or "",
        "model_nm": row.model_nm or "",
        "cur_stat": row.cur_stat or "",
        "err_msg": row.err_msg or "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }
    if row.zone_id is not None:
        kwargs["zone_id"] = row.zone_id
    return management_pb2.EquipmentEntry(**kwargs)


def stage_to_proto(row):
    return management_pb2.StageEntry(
        zone_id=row.zone_id,
        zone_nm=row.zone_nm or "",
        in_progress_count=row.in_progress_count,
    )


def order_item_progress_to_proto(row):
    return management_pb2.OrderItemProgressEntry(
        ord_id=row.ord_id,
        total_items=row.total_items,
        by_stat=row.by_stat,
    )


def schedule_job_to_proto(row):
    return management_pb2.ScheduleJob(
        id=row.id or "",
        order_id=row.order_id or "",
        priority_score=row.priority_score or 0.0,
        priority_rank=row.priority_rank or 0,
        assigned_stage=row.assigned_stage or "",
        status=row.status or "",
        estimated_completion=row.estimated_completion or "",
        started_at=row.started_at or "",
        completed_at=row.completed_at or "",
        created_at=row.created_at or "",
        company_name=row.company_name or "",
        customer_name=row.customer_name or "",
        total_amount=row.total_amount or 0.0,
        requested_delivery=row.requested_delivery or "",
        confirmed_delivery=row.confirmed_delivery or "",
        order_status=row.order_status or "",
    )


def priority_result_to_proto(row):
    return management_pb2.PriorityResult(
        order_id=row.order_id or "",
        company_name=row.company_name or "",
        product_summary=row.product_summary or "",
        total_quantity=row.total_quantity or 0,
        requested_delivery=row.requested_delivery or "",
        total_score=row.total_score or 0.0,
        rank=row.rank or 0,
        factors=[
            management_pb2.PriorityFactor(
                name=f.name or "",
                score=f.score or 0.0,
                max_score=f.max_score or 0.0,
                detail=f.detail or "",
            )
            for f in row.factors
        ],
        recommendation_reason=row.recommendation_reason or "",
        delay_risk=row.delay_risk or "",
        ready_status=row.ready_status or "",
        blocking_reasons=row.blocking_reasons,
        estimated_days=row.estimated_days or 0,
    )


def start_result_to_proto(r):
    """StartProductionResult dataclass -> proto."""
    return management_pb2.StartProductionResult(
        ord_id=r.ord_id,
        item_id=r.item_id,
        equip_task_txn_id=r.equip_task_txn_id,
        message=r.message or "",
    )


def start_result_to_order_ack(r, *, accepted=True, reason=""):
    """StartProductionResult dataclass -> canonical StartProductionOrderAck proto."""
    return management_pb2.StartProductionOrderAck(
        ord_id=r.ord_id,
        accepted=accepted,
        reason=reason or r.message or "",
        item_id=r.item_id,
        equip_task_txn_id=r.equip_task_txn_id,
    )


def build_batch_start_ack(order_ids, results):
    """Build per-order batch ack while keeping legacy TaskManager behavior.

    Legacy TaskManager currently returns only successful rows and silently skips
    invalid/missing/unregistered orders. For the skeleton response we preserve
    that behavior but expose it as explicit per-order accepted/rejected results.
    """
    normalized_order_ids = list(order_ids)
    accepted_by_ord_id = {int(r.ord_id): r for r in results}
    order_acks = []
    accepted_count = 0

    for raw in normalized_order_ids:
        try:
            parsed = int(str(raw).strip())
        except ValueError:
            order_acks.append(
                management_pb2.StartProductionOrderAck(
                    ord_id=0,
                    accepted=False,
                    reason=f"invalid order_id: {raw}",
                )
            )
            continue

        result = accepted_by_ord_id.get(parsed)
        if result is None:
            order_acks.append(
                management_pb2.StartProductionOrderAck(
                    ord_id=parsed,
                    accepted=False,
                    reason="rejected_or_skipped_by_legacy_path",
                )
            )
            continue

        accepted_count += 1
        order_acks.append(start_result_to_order_ack(result))

    requested_count = len(normalized_order_ids)
    rejected_count = requested_count - accepted_count
    return management_pb2.StartProductionAck(
        requested_count=requested_count,
        accepted_count=accepted_count,
        rejected_count=rejected_count,
        orders=order_acks,
        message=f"{accepted_count}/{requested_count} orders accepted",
    )


def result_to_legacy_work_order(r):
    """smartcast start_production_single result -> legacy WorkOrder proto."""
    return management_pb2.WorkOrder(
        id=r.item_id,
        order_id=str(r.ord_id),
        pattern_id="",
        qty=1,
        status=_WO_STATUS_TO_ENUM.get("QUE", 0),
        plan_start=_ts(None),
        act_start=_ts(None),
        act_end=_ts(None),
    )
