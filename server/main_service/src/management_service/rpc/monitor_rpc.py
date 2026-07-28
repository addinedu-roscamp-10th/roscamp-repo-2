"""Monitoring stream RPC methods."""

from __future__ import annotations

import grpc
import management_pb2  # type: ignore


class MonitorRpcMixin:
    """Execution monitor streaming RPCs."""

    def WatchItems(self, request, context):
        order_filter = request.order_id or None
        for event in self.execution_monitor.stream(order_filter):
            if context.is_active() is False:
                break
            yield event

    def WatchAlerts(self, request, context):
        sev = request.severity_filter or None
        for event in self.execution_monitor.stream_alerts(sev):
            if context.is_active() is False:
                break
            yield event

    def ListAlerts(self, request, context):
        """alerts_stat 테이블에서 최근 알림 목록 반환."""
        from smart_cast_db.models import Alert
        from smart_cast_db.database import SessionLocal

        sev = (request.severity_filter or "").strip().lower() or None
        limit = request.limit if request.limit > 0 else 20

        try:
            with SessionLocal() as db:
                q = db.query(Alert).order_by(Alert.timestamp.desc())
                if sev:
                    q = q.filter(Alert.severity == sev)
                rows = q.limit(limit).all()

            alerts = [
                management_pb2.AlertEvent(
                    id=r.id,
                    type=r.type or "",
                    severity=r.severity or "info",
                    error_code=r.error_code or "",
                    message=r.message or "",
                    equipment_id=r.res_id or "",
                    zone=r.zone or "",
                    at=management_pb2.Timestamp(iso8601=r.timestamp or ""),
                )
                for r in rows
            ]
            return management_pb2.ListAlertsResponse(alerts=alerts)
        except Exception as exc:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return management_pb2.ListAlertsResponse()
