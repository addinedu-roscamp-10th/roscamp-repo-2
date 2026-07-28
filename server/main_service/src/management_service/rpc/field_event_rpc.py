"""Field event ingestion RPC methods."""

from __future__ import annotations

import logging

import grpc
import management_pb2  # type: ignore

logger = logging.getLogger(__name__)


def _item_state_label(item) -> str:
    return getattr(item, "flow_stat", None) or ""


def _pp_option_proto(option: dict) -> management_pb2.PpOptionView:
    return management_pb2.PpOptionView(
        pp_id=int(option.get("pp_id") or 0),
        pp_nm=option.get("pp_nm") or "",
        extra_cost=float(option.get("extra_cost") or 0.0),
        txn_stat=str(option.get("txn_stat") or ""),
        txn_id=int(option.get("txn_id") or 0),
        map_id=int(option.get("map_id") or 0),
    )


def _build_rfid_ack_details(item_id: int) -> tuple[str, int, list[management_pb2.PpOptionView]]:
    from services.legacy.handoff_pipeline import build_pp_options_view

    from smart_cast_db.database import SessionLocal
    from smart_cast_db.models import ItemStat

    item_cur_stat = ""
    ord_id_int = 0
    pp_options_proto: list[management_pb2.PpOptionView] = []
    db = SessionLocal()
    try:
        item = db.get(ItemStat, item_id)
        if item is not None:
            item_cur_stat = _item_state_label(item)
            ord_id_int = int(item.ord_id) if item.ord_id else 0
            for opt in build_pp_options_view(db, item):
                pp_options_proto.append(_pp_option_proto(opt))
    finally:
        db.close()
    return item_cur_stat, ord_id_int, pp_options_proto


class FieldEventRpcMixin:
    """Handoff, RFID, and conveyor event RPCs."""

    def ReportHandoffAck(self, request, context):
        zone = request.zone or "postprocessing"
        source_device = request.source_device or "unknown"
        idempotency_key = request.idempotency_key or None
        operator_id = request.operator_id if request.HasField("operator_id") else None
        result = self.handoff_command_service.report(
            source_device=source_device,
            zone=zone,
            idempotency_key=idempotency_key,
            operator_id=operator_id,
        )

        return management_pb2.HandoffAckResponse(
            accepted=result.accepted,
            task_id=result.task_id,
            amr_id=result.amr_id,
            reason=result.reason,
            ack_at=result.ack_at.isoformat(),
            released=result.released,
            item_id=result.item_id or 0,
            ord_id=result.ord_id or 0,
        )

    def ReportRfidScan(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("ReportRfidScan is disabled")
        return management_pb2.RfidScanAck()

    def ReportConveyorEvent(self, request, context):
        from services.legacy.handoff_pipeline import apply_tof1, apply_tof2

        from smart_cast_db.database import SessionLocal

        res_id = request.res_id or "CONV1"
        event = (request.event_type or "").strip().lower()
        rfid_payload = request.rfid_payload or None
        item_id = int(request.item_id) if request.item_id else None

        if event not in ("tof1_entry", "tof2_exit"):
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"unknown event_type={event!r} (expected tof1_entry|tof2_exit)")
            return management_pb2.ConveyorEventAck()

        db = SessionLocal()
        try:
            try:
                if event == "tof1_entry":
                    r1 = apply_tof1(db, res_id=res_id, rfid_payload=rfid_payload, item_id=item_id)
                    db.commit()
                    return management_pb2.ConveyorEventAck(
                        accepted=r1.ok,
                        item_id=int(r1.item_id or 0),
                        item_cur_stat=r1.item_cur_stat or "",
                        equip_task_txn_id=int(r1.equip_task_txn_id or 0),
                        insp_task_txn_id=0,
                        reason=r1.reason,
                    )
                r2 = apply_tof2(db, res_id=res_id, item_id=item_id)
                db.commit()
                return management_pb2.ConveyorEventAck(
                    accepted=r2.ok,
                    item_id=int(r2.item_id or 0),
                    item_cur_stat=r2.item_cur_stat or "",
                    equip_task_txn_id=int(r2.equip_task_txn_succ_id or 0),
                    insp_task_txn_id=int(r2.insp_task_txn_id or 0),
                    reason=r2.reason,
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.exception("ReportConveyorEvent 실패 res=%s event=%s: %s", res_id, event, exc)
                context.set_code(grpc.StatusCode.INTERNAL)
                context.set_details(str(exc))
                return management_pb2.ConveyorEventAck()
        finally:
            db.close()
