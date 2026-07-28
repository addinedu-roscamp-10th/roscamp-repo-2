"""Management gRPC 핸드오프 명령 처리."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from smart_cast_db.database import SessionLocal
from smart_cast_db.models.models_legacy import HandoffAck

from services.contracts.enums import EventType
from services.contracts.models import Event
from services.legacy.handoff_pipeline import apply_handoff


@dataclass(frozen=True)
class HandoffCommandResult:
    accepted: bool
    task_id: str
    amr_id: str
    reason: str
    ack_at: datetime
    released: bool
    item_id: int | None
    ord_id: int | None


def _extra_int(extra: object, key: str) -> int | None:
    if not isinstance(extra, dict):
        return None
    value = extra.get(key)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class HandoffCommandService:
    """중복 검사와 DB 변경을 마친 뒤 대기 해제 이벤트를 발행."""

    def __init__(
        self,
        event_bridge: Any,
        session_factory: Callable[[], Any] = SessionLocal,
        apply_func: Callable[..., Any] = apply_handoff,
    ) -> None:
        self._event_bridge = event_bridge
        self._session_factory = session_factory
        self._apply_func = apply_func

    def report(
        self,
        *,
        source_device: str,
        zone: str,
        idempotency_key: str | None,
        operator_id: int | None,
    ) -> HandoffCommandResult:
        with self._session_factory() as db:
            if idempotency_key:
                duplicate = (
                    db.query(HandoffAck)
                    .filter(HandoffAck.idempotency_key == idempotency_key)
                    .first()
                )
                if duplicate is not None:
                    return HandoffCommandResult(
                        accepted=True,
                        task_id=str(
                            _extra_int(duplicate.extra, "trans_task_txn_id") or ""
                        ),
                        amr_id=duplicate.amr_id or "",
                        reason="duplicate",
                        ack_at=duplicate.ack_at or datetime.now(timezone.utc),
                        released=not duplicate.orphan_ack,
                        item_id=_extra_int(duplicate.extra, "item_id"),
                        ord_id=_extra_int(duplicate.extra, "ord_id"),
                    )

            applied = self._apply_func(
                db,
                button_device_id=source_device,
                ack_source="gui_override",
                via="grpc",
                idempotency_key=idempotency_key,
                operator_id=operator_id,
                zone=zone,
            )
            db.commit()

        if applied.released and applied.item_id is not None:
            self._event_bridge.publish(
                Event(
                    event_type=EventType.SUBTASK_COMPLETED,
                    item_id=int(applied.item_id),
                    res_id=applied.amr_id,
                    payload={
                        "subtask_type": EventType.HANDOFF_ACK.value,
                        "source": "management.report_handoff_ack",
                        "resolved_via": "apply_handoff",
                    },
                )
            )

        return HandoffCommandResult(
            accepted=True,
            task_id=str(applied.task_id or ""),
            amr_id=applied.amr_id or "",
            reason="released" if applied.released else applied.reason,
            ack_at=datetime.now(timezone.utc),
            released=applied.released,
            item_id=applied.item_id,
            ord_id=applied.ord_id,
        )
