"""EventGateway gRPC client (PyQt) — sole channel to backend EventBridge.

PyQt(Monitoring Service) ↔ Management Service 통신은 EventGateway gRPC 만 사용 (TCP 채널,
HTTP 호출 없음 — 아키텍처 다이어그램 준수).

publish (PyQt → backend):
    HANDOFF_ACK             ① 핸드오프 ACK 버튼
    ITEM_LOOKUP_REQUESTED   ② RFID 스캔 버튼 (raw_payload 로 item 조회 요청)
    PP_DONE_REQUESTED       ③ 후처리 완료 버튼

subscribe (backend → PyQt) — WatchEvents server-streaming:
    RFID_SCANNED            Jetson hardware RFID NDEF 스캔 → _payload_edit 자동 채움
    ITEM_LOOKUP_RESULT      ② 응답 (item + pp_options) → 화면 정보 표시
    TOF1_ENTRY              카메라 앞 정지 센서 ON edge → indicator 갱신

설계:
    - publish: 모듈 레벨 lazy singleton — 첫 호출 시 channel open. silent fail.
    - subscribe: WatchEventsThread (QThread) + pyqtSignal(dict) — main thread 에 GUI 변경 dispatch.
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


# ----------------------------------------------------------------------------
# WatchEvents (server-streaming) — Qt-friendly subscriber thread
# ----------------------------------------------------------------------------

WATCH_RECONNECT_SEC = 3.0


class WatchEventsThread:
    """EventGateway WatchEvents subscriber — Qt thread + pyqtSignal.

    PyQt 가 backend EventBridge 의 stream 을 받기 위한 채널. 별도 QThread 에서
    blocking gRPC stream 을 소비하고 ``event_received`` signal 로 main thread (GUI) 에
    이벤트 dispatch. GUI 변경은 slot 에서 (main thread) 안전하게 수행.

    Args:
        event_types: 받을 EventType 화이트리스트 (예: ["RFID_SCANNED", "TOF1_ENTRY"]).
        consumer: 디버깅용 식별자 (default: "pyqt-monitoring").

    Signals:
        event_received(dict): 디코드된 이벤트 dict — keys: event_type, resource_id,
            source, occurred_at_iso, idempotency_key, payload (dict).

    Usage:
        from PyQt5.QtCore import QObject
        watcher = WatchEventsThread(["RFID_SCANNED", "TOF1_ENTRY", "ITEM_LOOKUP_RESULT"])
        watcher.event_received.connect(self._on_event)  # main thread slot
        watcher.start()
        ...
        watcher.stop()  # 앱 종료 시
    """

    def __init__(
        self,
        event_types: list[str],
        consumer: str = "pyqt-monitoring",
    ) -> None:
        # PyQt 의 QThread + pyqtSignal 임포트 — 모듈 레벨 import 안 함 (cli 환경 호환).
        from PyQt5.QtCore import QObject, QThread, pyqtSignal

        self._event_types = list(event_types)
        self._consumer = consumer
        self._stop_event = threading.Event()
        self._target: str | None = None

        # Qt signal emitter (QObject 상속 inner class).
        class _Emitter(QObject):
            event_received = pyqtSignal(dict)

        # Worker QThread inner class.
        outer = self

        class _Worker(QThread):
            def run(self) -> None:  # type: ignore[override]
                outer._run_loop(emitter=outer.emitter)

        self.emitter = _Emitter()
        self.event_received = self.emitter.event_received  # 외부 connect 용
        self._thread = _Worker()

    def start(self) -> None:
        """thread 시작 — backend 채널 lazy resolve."""
        if not _ensure_init():
            logger.info(
                "WatchEventsThread: EventGateway 비활성 — start skip event_types=%s",
                self._event_types,
            )
            return
        self._target = _target
        self._thread.start()
        logger.info(
            "WatchEventsThread 시작: event_types=%s consumer=%s target=%s",
            self._event_types, self._consumer, self._target,
        )

    def stop(self) -> None:
        """stop 신호 + thread join — 앱 종료 시 호출."""
        self._stop_event.set()
        try:
            self._thread.quit()
            self._thread.wait(2000)
        except Exception:  # noqa: BLE001
            pass

    def _run_loop(self, emitter) -> None:
        """gRPC stream 소비 루프 — 끊김 시 backoff 재연결."""
        try:
            import grpc
            from google.protobuf.json_format import MessageToDict

            import event_gateway_pb2 as eg_pb  # type: ignore
            import event_gateway_pb2_grpc as eg_pb_grpc  # type: ignore
        except Exception as exc:  # noqa: BLE001
            logger.warning("WatchEventsThread import 실패: %s", exc)
            return

        target = self._target
        if not target:
            return

        while not self._stop_event.is_set():
            channel = None
            try:
                channel = grpc.insecure_channel(target)
                stub = eg_pb_grpc.EventGatewayStub(channel)
                req = eg_pb.WatchEventsRequest(
                    event_types=self._event_types,
                    consumer=self._consumer,
                )
                stream = stub.WatchEvents(req)
                logger.info(
                    "WatchEvents stream 활성 event_types=%s", self._event_types,
                )
                for envelope in stream:
                    if self._stop_event.is_set():
                        break
                    payload_dict: dict[str, Any] = {}
                    if envelope.HasField("payload"):
                        payload_dict = MessageToDict(
                            envelope.payload, preserving_proto_field_name=True,
                        )
                    decoded = {
                        "event_type": envelope.event_type,
                        "resource_id": envelope.resource_id,
                        "source": envelope.source,
                        "idempotency_key": envelope.idempotency_key,
                        "payload": payload_dict,
                    }
                    if envelope.HasField("occurred_at"):
                        ts = envelope.occurred_at.ToDatetime(tzinfo=timezone.utc)
                        decoded["occurred_at_iso"] = ts.isoformat()
                    emitter.event_received.emit(decoded)
            except grpc.RpcError as exc:  # type: ignore[attr-defined]
                if self._stop_event.is_set():
                    break
                logger.warning(
                    "WatchEvents stream 끊김 — %.1fs 후 재연결: %s",
                    WATCH_RECONNECT_SEC, exc,
                )
                self._stop_event.wait(WATCH_RECONNECT_SEC)
            except Exception as exc:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                logger.warning(
                    "WatchEvents 예외 — %.1fs 후 재연결: %s",
                    WATCH_RECONNECT_SEC, exc,
                )
                self._stop_event.wait(WATCH_RECONNECT_SEC)
            finally:
                if channel is not None:
                    try:
                        channel.close()
                    except Exception:  # noqa: BLE001
                        pass
