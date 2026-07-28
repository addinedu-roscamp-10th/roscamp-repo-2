"""EventGateway gRPC client (Jetson) — sole channel to backend EventBridge.

Wire (proto/event_gateway.proto):
    PublishEvent (unary)         외부 → backend EventBridge.publish
    WatchEvents (server stream)  backend EventBridge → 외부 (filter 기반)

설계:
    - EventGatewayClient(host, port).start() 시 backend 채널 open + WatchEvents thread 시작.
    - publish_event(...): unary 호출, fail 시 warning 로그만 (hardware 신호 손실은 다음 edge 에서 회복).
    - subscribe(event_type, callback): 등록 후 WatchEvents stream 에 자동 합류.
      stream 끊기면 backoff 재연결.

Jetson 환경: grpcio 1.59.5 (JetPack default). 본 모듈은 1.59 호환 API 만 사용.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any, Callable

import grpc
from google.protobuf import struct_pb2, timestamp_pb2
from google.protobuf.json_format import ParseDict

# generated/ sys.path 추가 (publisher.py 와 동일 패턴)
_GEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated")
if _GEN_DIR not in sys.path:
    sys.path.insert(0, _GEN_DIR)

import event_gateway_pb2 as eg_pb  # type: ignore  # noqa: E402
import event_gateway_pb2_grpc as eg_pb_grpc  # type: ignore  # noqa: E402

log = logging.getLogger("jetson.event_gateway")

# WatchEvents stream 끊김 후 재연결 backoff 초.
WATCH_RECONNECT_SEC = 3.0
# publish 호출 timeout (초).
PUBLISH_TIMEOUT_SEC = 2.0


class EventGatewayClient:
    """Backend EventGateway 와 통신하는 sole client.

    publish: hardware 신호 → backend
    subscribe: backend → hardware (콜백 호출, 별도 thread)

    Args:
        target: "host:port" (예: "100.77.239.25:50051").
        source: 본 client 의 식별자 (예: "jetson-orin-nx-01"). 모든 publish 의 source 필드.
        consumer: WatchEvents 의 consumer 식별자 (디버깅 용).
        shutdown_event: 외부 SIGTERM 동기화. None 이면 자체 생성.
    """

    def __init__(
        self,
        *,
        target: str,
        source: str,
        consumer: str | None = None,
        shutdown_event: threading.Event | None = None,
    ) -> None:
        self._target = target
        self._source = source
        self._consumer = consumer or source
        self._shutdown = shutdown_event or threading.Event()
        self._channel: grpc.Channel | None = None
        self._stub: eg_pb_grpc.EventGatewayStub | None = None
        # subscribe(event_type, callback) 로 등록된 핸들러.
        # 동일 event_type 다중 callback 가능.
        self._callbacks: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._cb_lock = threading.Lock()
        self._watch_thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        """gRPC 채널 open + (callback 등록되어 있으면) WatchEvents thread 시작."""
        if self._started:
            return
        self._channel = grpc.insecure_channel(self._target)
        self._stub = eg_pb_grpc.EventGatewayStub(self._channel)
        self._started = True
        log.info(
            "EventGatewayClient 시작: target=%s source=%s consumer=%s",
            self._target, self._source, self._consumer,
        )
        # callback 이 이미 등록되어 있으면 즉시 stream thread 시작
        if self._callbacks and self._watch_thread is None:
            self._start_watch_thread()

    def stop(self) -> None:
        """채널 종료 + WatchEvents thread join."""
        self._shutdown.set()
        if self._watch_thread is not None and self._watch_thread.is_alive():
            self._watch_thread.join(timeout=2.0)
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:  # noqa: BLE001
                pass
        self._started = False
        log.info("EventGatewayClient 종료")

    # ---- publish (unary) ----------------------------------------------------

    def publish_event(
        self,
        *,
        event_type: str,
        resource_id: str = "",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> bool:
        """단일 hardware 신호 publish. 실패 시 warning 로그 + False.

        idempotency_key 비어있으면 자동 UUID 생성 (네트워크 retry 안전).
        """
        if not self._started or self._stub is None:
            log.warning("publish_event 호출 전 start() 필요 — skip event_type=%s", event_type)
            return False
        if not idempotency_key:
            idempotency_key = uuid.uuid4().hex
        payload_struct = struct_pb2.Struct()
        if payload:
            try:
                ParseDict(payload, payload_struct)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "publish_event: payload 직렬화 실패 event_type=%s reason=%s — drop",
                    event_type, exc,
                )
                return False
        ts = timestamp_pb2.Timestamp()
        if occurred_at is not None:
            ts.FromDatetime(
                occurred_at.astimezone(timezone.utc).replace(tzinfo=None)
            )
        else:
            ts.FromDatetime(datetime.now(timezone.utc).replace(tzinfo=None))
        envelope = eg_pb.EventEnvelope(
            event_type=event_type,
            resource_id=resource_id,
            source=self._source,
            occurred_at=ts,
            idempotency_key=idempotency_key,
            payload=payload_struct,
        )
        req = eg_pb.PublishEventRequest(event=envelope)
        try:
            resp = self._stub.PublishEvent(req, timeout=PUBLISH_TIMEOUT_SEC)
        except grpc.RpcError as exc:
            log.warning(
                "publish_event RPC 실패 event_type=%s code=%s details=%s",
                event_type, exc.code() if hasattr(exc, "code") else "?",
                exc.details() if hasattr(exc, "details") else str(exc),
            )
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("publish_event 예외 event_type=%s: %s", event_type, exc)
            return False
        if resp.deduplicated:
            log.info(
                "publish_event dedup hit event_type=%s idem=%s — backend 1회 처리",
                event_type, idempotency_key,
            )
        elif resp.accepted:
            log.info("publish_event OK event_type=%s idem=%s", event_type, idempotency_key)
        else:
            log.warning(
                "publish_event 거부 event_type=%s reason=%s",
                event_type, resp.reason,
            )
        return resp.accepted

    # ---- subscribe (server-streaming) ---------------------------------------

    def subscribe(
        self,
        event_type: str,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """callback 등록. start() 후 호출하면 즉시 stream thread 가 받기 시작.

        callback 인자: 디코드된 dict (event_type, resource_id, source, occurred_at_iso,
        idempotency_key, payload).
        """
        with self._cb_lock:
            self._callbacks.setdefault(event_type, []).append(callback)
        if self._started and self._watch_thread is None:
            self._start_watch_thread()

    def _start_watch_thread(self) -> None:
        if self._watch_thread is not None:
            return
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            name="event-gateway-watch",
            daemon=True,
        )
        self._watch_thread.start()

    def _watch_loop(self) -> None:
        """WatchEvents stream 소비. 끊김 시 backoff 후 재연결."""
        while not self._shutdown.is_set():
            with self._cb_lock:
                event_types = sorted(self._callbacks.keys())
            if not event_types:
                self._shutdown.wait(WATCH_RECONNECT_SEC)
                continue
            req = eg_pb.WatchEventsRequest(
                event_types=event_types,
                consumer=self._consumer,
            )
            try:
                assert self._stub is not None
                stream = self._stub.WatchEvents(req)
                log.info(
                    "EventGatewayClient WatchEvents 시작 event_types=%s consumer=%s",
                    event_types, self._consumer,
                )
                for envelope in stream:
                    if self._shutdown.is_set():
                        break
                    self._dispatch(envelope)
            except grpc.RpcError as exc:
                if self._shutdown.is_set():
                    return
                code = exc.code() if hasattr(exc, "code") else "?"
                log.warning(
                    "WatchEvents stream 끊김 code=%s — %.1fs 후 재연결",
                    code, WATCH_RECONNECT_SEC,
                )
                self._shutdown.wait(WATCH_RECONNECT_SEC)
            except Exception as exc:  # noqa: BLE001
                if self._shutdown.is_set():
                    return
                log.warning(
                    "WatchEvents 예외 %s — %.1fs 후 재연결",
                    exc, WATCH_RECONNECT_SEC,
                )
                self._shutdown.wait(WATCH_RECONNECT_SEC)

    def _dispatch(self, envelope: "eg_pb.EventEnvelope") -> None:
        """수신 envelope → 등록된 callback 호출."""
        et = envelope.event_type
        with self._cb_lock:
            callbacks = list(self._callbacks.get(et, []))
        if not callbacks:
            return
        from google.protobuf.json_format import MessageToDict

        payload_dict: dict[str, Any] = {}
        if envelope.HasField("payload"):
            payload_dict = MessageToDict(
                envelope.payload, preserving_proto_field_name=True,
            )
        ts_iso = ""
        if envelope.HasField("occurred_at"):
            ts_iso = envelope.occurred_at.ToDatetime(tzinfo=timezone.utc).isoformat()
        decoded = {
            "event_type": et,
            "resource_id": envelope.resource_id,
            "source": envelope.source,
            "occurred_at_iso": ts_iso,
            "idempotency_key": envelope.idempotency_key,
            "payload": payload_dict,
        }
        for cb in callbacks:
            try:
                cb(decoded)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "WatchEvents callback 예외 event_type=%s: %s", et, exc,
                )


# ----------------------------------------------------------------------------
# Module-level singleton helper — esp_bridge / publisher 가 한 번만 시작.
# ----------------------------------------------------------------------------

_singleton: EventGatewayClient | None = None
_singleton_lock = threading.Lock()


def get_singleton() -> EventGatewayClient | None:
    """현재 등록된 client 반환 (있으면). 없으면 None."""
    return _singleton


def init_singleton(
    *,
    target: str | None = None,
    source: str | None = None,
    shutdown_event: threading.Event | None = None,
) -> EventGatewayClient | None:
    """env 기반으로 singleton 시작. 이미 있으면 그대로 반환.

    환경 변수:
        EVENT_GATEWAY_TARGET   — backend "host:port" (default: MANAGEMENT_GRPC_TARGET)
        EVENT_GATEWAY_SOURCE   — client 식별자 (default: hostname)
        EVENT_GATEWAY_ENABLED  — "0"/"false" 면 비활성 (None 반환)
    """
    global _singleton
    enabled = os.environ.get("EVENT_GATEWAY_ENABLED", "1").strip().lower()
    if enabled in ("0", "false", "no"):
        log.info("EventGatewayClient: EVENT_GATEWAY_ENABLED=0 — 비활성")
        return None
    with _singleton_lock:
        if _singleton is not None:
            return _singleton
        target = (
            target
            or os.environ.get("EVENT_GATEWAY_TARGET", "").strip()
            or os.environ.get("MANAGEMENT_GRPC_TARGET", "").strip()
        )
        if not target:
            log.warning(
                "EventGatewayClient: target 미지정 — EVENT_GATEWAY_TARGET 또는 "
                "MANAGEMENT_GRPC_TARGET 환경변수 필요. 비활성.",
            )
            return None
        if not source:
            try:
                import socket as _socket

                source = (
                    os.environ.get("EVENT_GATEWAY_SOURCE", "").strip()
                    or _socket.gethostname()
                    or "jetson-unknown"
                )
            except Exception:  # noqa: BLE001
                source = "jetson-unknown"
        client = EventGatewayClient(
            target=target,
            source=source,
            consumer=source,
            shutdown_event=shutdown_event,
        )
        client.start()
        _singleton = client
        return _singleton


def stop_singleton() -> None:
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.stop()
            _singleton = None
