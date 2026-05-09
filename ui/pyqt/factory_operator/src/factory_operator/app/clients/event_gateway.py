"""EventGateway gRPC client (PyQt) — sole channel to backend EventBridge.

Publish-only (PyQt UI 버튼 → backend EventBridge). Subscribe 는 Phase 3 다음 단계에서
필요 시 별도 thread + signal 로 추가.

지원 EventType (publish):
    HANDOFF_ACK       — ① 핸드오프 ACK 버튼
    PP_DONE_REQUESTED — ③ 후처리 완료 버튼

설계:
    - 모듈 레벨 lazy singleton — 첫 publish 시 channel open.
    - publish 실패는 silent warning (UX 흐름은 backend HTTP 호출 등 기존 channel 보존).
    - EVENT_GATEWAY_TARGET / MANAGEMENT_GRPC_TARGET 미설정이면 비활성.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("monitoring.event_gateway")

# generated/ sys.path 추가 (proto stub import 호환)
_GEN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "generated",
)
if _GEN_DIR not in sys.path:
    sys.path.insert(0, _GEN_DIR)


_lock = threading.Lock()
_channel: Any | None = None
_stub: Any | None = None
_target: str | None = None
_source: str | None = None
_disabled: bool = False  # init 실패 후 재시도 안 함

PUBLISH_TIMEOUT_SEC = 2.0


def _ensure_init() -> bool:
    """Lazy init — 첫 publish 시 channel 생성. 실패 시 _disabled = True."""
    global _channel, _stub, _target, _source, _disabled
    if _disabled:
        return False
    if _stub is not None:
        return True
    with _lock:
        if _stub is not None:
            return True
        # env 우선
        target = (
            os.environ.get("EVENT_GATEWAY_TARGET", "").strip()
            or os.environ.get("MANAGEMENT_GRPC_TARGET", "").strip()
        )
        if not target:
            logger.info(
                "EventGateway 비활성 — EVENT_GATEWAY_TARGET / MANAGEMENT_GRPC_TARGET 미설정",
            )
            _disabled = True
            return False
        try:
            import grpc  # lazy
            import event_gateway_pb2_grpc as eg_pb_grpc  # type: ignore

            channel = grpc.insecure_channel(target)
            stub = eg_pb_grpc.EventGatewayStub(channel)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventGateway init 실패 target=%s: %s", target, exc)
            _disabled = True
            return False
        _channel = channel
        _stub = stub
        _target = target
        _source = (
            os.environ.get("EVENT_GATEWAY_SOURCE", "").strip()
            or "pyqt-monitoring"
        )
        logger.info(
            "EventGateway client 활성 target=%s source=%s", target, _source,
        )
        return True


def publish_event(
    *,
    event_type: str,
    resource_id: str = "",
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> bool:
    """단일 publish. 실패 / 비활성 시 False (silent — UX 영향 없음)."""
    if not _ensure_init():
        return False
    try:
        from google.protobuf import struct_pb2, timestamp_pb2
        from google.protobuf.json_format import ParseDict
        import event_gateway_pb2 as eg_pb  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning("EventGateway proto import 실패: %s", exc)
        return False

    if not idempotency_key:
        idempotency_key = uuid.uuid4().hex
    payload_struct = struct_pb2.Struct()
    if payload:
        try:
            ParseDict(payload, payload_struct)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventGateway payload 직렬화 실패: %s", exc)
            return False
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(datetime.now(timezone.utc).replace(tzinfo=None))
    envelope = eg_pb.EventEnvelope(
        event_type=event_type,
        resource_id=resource_id,
        source=_source or "pyqt-monitoring",
        occurred_at=ts,
        idempotency_key=idempotency_key,
        payload=payload_struct,
    )
    req = eg_pb.PublishEventRequest(event=envelope)
    try:
        assert _stub is not None
        resp = _stub.PublishEvent(req, timeout=PUBLISH_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "EventGateway publish RPC 실패 event_type=%s: %s", event_type, exc,
        )
        return False
    if resp.deduplicated:
        logger.info(
            "EventGateway dedup hit event_type=%s idem=%s", event_type, idempotency_key,
        )
    elif not resp.accepted:
        logger.warning(
            "EventGateway publish 거부 event_type=%s reason=%s",
            event_type, resp.reason,
        )
    return resp.accepted


def shutdown() -> None:
    """앱 종료 시 channel close. PyQt closeEvent 등에서 호출."""
    global _channel, _stub
    with _lock:
        if _channel is not None:
            try:
                _channel.close()
            except Exception:  # noqa: BLE001
                pass
        _channel = None
        _stub = None
