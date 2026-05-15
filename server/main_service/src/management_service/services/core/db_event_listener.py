"""PostgreSQL LISTEN/NOTIFY 기반 DB row trigger event bridge — Phase 1 골격.

SPEC: docs/db_event_bridge/SPEC.md

흐름:
    [DB AFTER INSERT/UPDATE trigger]
        pg_notify('lifecycle_event', json_payload)
        ↓
    [DbEventListener (asyncpg LISTEN)]
        raw NOTIFY → JSON 파싱 → Event(DB_ROW_CHANGED) 변환
        ↓
    [EventBridge.publish()]
        기존 in-process subscriber (task_executor 등) 가 동일하게 라우팅 받음

설계 원칙:
    - asyncpg 비동기 connection — backend uvicorn event loop 와 통합
    - graceful shutdown — `asyncio.Event` 로 task cleanup
    - 자동 재연결 — connection 끊김 시 지수 백오프 (initial 1s → max 5min)
    - feature flag `MGMT_DB_EVENT_BRIDGE_ENABLED=1` 로 옵트인 (Phase 2 dual delivery 안전성)
    - DB / event_bridge 미설정 시 silent skip (start() 가 no-op)

환경 변수:
    MGMT_DB_EVENT_BRIDGE_ENABLED   1 / true / yes 일 때만 활성 (기본 off)
    MGMT_DB_EVENT_CHANNEL          LISTEN 채널명 (기본 lifecycle_event)
    DATABASE_URL                   asyncpg 호환 dsn (postgresql://user:pw@host/db)

@MX:NOTE: Phase 1 골격 - subscriber 측은 EventType.DB_ROW_CHANGED 의 payload.table 로 분기.
@MX:TODO: Phase 2 에서 table 별 specific EventType 추가 + DB trigger SQL 마이그레이션.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

import asyncpg

from services.contracts.enums import EventType
from services.contracts.models import Event

if TYPE_CHECKING:
    from services.core.event_bridge import EventBridgeImpl

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL = "lifecycle_event"
_RECONNECT_INITIAL_SEC = 1.0
_RECONNECT_MAX_SEC = 300.0  # 5분 상한

# asyncpg 는 dsn 의 SQLAlchemy +driver 접미사 (postgresql+psycopg) 를 인식 못 함 — 제거 필요.
_SQLA_DRIVER_PREFIX = re.compile(r"^(postgres(?:ql)?)\+[a-z0-9_]+(://)")


def _normalize_dsn(dsn: str) -> str:
    """SQLAlchemy URL (`postgresql+psycopg://...`) 을 asyncpg 호환 (`postgresql://...`) 로 변환."""
    return _SQLA_DRIVER_PREFIX.sub(r"\1\2", dsn)


class DbEventListener:
    """PostgreSQL LISTEN/NOTIFY listener — DB row trigger 를 EventBridge 로 라우팅.

    feature flag `MGMT_DB_EVENT_BRIDGE_ENABLED=1` 일 때만 활성.
    container.startup 에서 `await listener.start()`, shutdown 에서 `await listener.stop()`.

    Phase 1 골격: NOTIFY payload 를 generic Event(DB_ROW_CHANGED) 로 변환.
    subscriber 는 payload["table"] / payload["op"] 로 분기.

    NOTIFY payload 예시 (SPEC §5.2):
        {
          "event": "insp_task_txn_insert",
          "schema": "smartcast",
          "table": "insp_task_txn",
          "op": "INSERT",
          "row": {"txn_id": 14, "item_id": 29, "txn_stat": "PROC", ...},
          "old_row": null,
          "at": "2026-05-15T09:18:23.731Z"
        }
    """

    def __init__(
        self,
        *,
        event_bridge: "EventBridgeImpl",
        dsn: str | None = None,
        channel: str | None = None,
    ) -> None:
        self._event_bridge = event_bridge
        raw_dsn = dsn or os.environ.get("DATABASE_URL", "")
        self._dsn = _normalize_dsn(raw_dsn) if raw_dsn else ""
        self._channel = channel or os.environ.get("MGMT_DB_EVENT_CHANNEL", _DEFAULT_CHANNEL)
        self._conn: asyncpg.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._reconnect_delay = _RECONNECT_INITIAL_SEC

    @staticmethod
    def _is_enabled() -> bool:
        return os.environ.get("MGMT_DB_EVENT_BRIDGE_ENABLED", "0") in ("1", "true", "yes")

    async def start(self) -> None:
        """Listener task 시작. feature flag off 또는 DSN 미설정 시 silent skip."""
        if not self._is_enabled():
            logger.info("DbEventListener: MGMT_DB_EVENT_BRIDGE_ENABLED off — skip")
            return
        if not self._dsn:
            logger.warning("DbEventListener: DATABASE_URL 미설정 — skip")
            return
        if self._task is not None and not self._task.done():
            logger.info("DbEventListener: 이미 실행 중 — skip start")
            return

        self._stop.clear()
        self._reconnect_delay = _RECONNECT_INITIAL_SEC
        self._task = asyncio.create_task(self._run_loop(), name="db_event_listener")
        logger.info("DbEventListener: started channel=%s", self._channel)

    async def stop(self) -> None:
        """Graceful shutdown — listener task 종료 + connection close."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                logger.warning("DbEventListener: stop timeout, task cancelled")
            self._task = None
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("DbEventListener: connection close 실패: %s", exc)
            self._conn = None
        logger.info("DbEventListener: stopped")

    async def _run_loop(self) -> None:
        """connection 끊김에 대비한 자동 재연결 루프."""
        while not self._stop.is_set():
            try:
                await self._connect_and_listen()
                self._reconnect_delay = _RECONNECT_INITIAL_SEC  # 성공 시 reset
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "DbEventListener: 연결 실패 (재시도 %.1fs 후): %s",
                    self._reconnect_delay,
                    exc,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._reconnect_delay)
                    return  # stop 시그널 받으면 종료
                except asyncio.TimeoutError:
                    pass
                self._reconnect_delay = min(self._reconnect_delay * 2, _RECONNECT_MAX_SEC)

    async def _connect_and_listen(self) -> None:
        self._conn = await asyncpg.connect(dsn=self._dsn)
        await self._conn.add_listener(self._channel, self._on_notify)
        logger.info("DbEventListener: LISTEN %s active", self._channel)
        try:
            # listener 가 backend task 로 동작 — connection 살아있는 동안 NOTIFY 수신.
            # stop 시그널 또는 connection 끊김으로 빠져나옴.
            await self._stop.wait()
        finally:
            try:
                await self._conn.remove_listener(self._channel, self._on_notify)
            except Exception:  # noqa: BLE001 — cleanup best-effort
                pass
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def _on_notify(
        self, conn: asyncpg.Connection, pid: int, channel: str, payload: str
    ) -> None:
        """asyncpg listener callback — sync. NOTIFY 1건 = 1 publish."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            logger.warning("DbEventListener: payload 파싱 실패: %s payload=%r", exc, payload[:200])
            return
        if not isinstance(data, dict):
            logger.warning("DbEventListener: payload 가 dict 아님: %r", payload[:200])
            return
        try:
            event = self._build_event(data)
            self._event_bridge.publish(event)
            logger.info(
                "DbEventListener: relay event=%s table=%s op=%s item_id=%s",
                data.get("event"),
                data.get("table"),
                data.get("op"),
                event.item_id,
            )
        except Exception as exc:  # noqa: BLE001 — handler 격리 (한 NOTIFY 실패가 listener 전체 멈춤 방지)
            logger.warning(
                "DbEventListener: event 변환/relay 실패: %s data=%r", exc, data,
            )

    def _build_event(self, data: dict[str, Any]) -> Event:
        """NOTIFY payload → Event(DB_ROW_CHANGED).

        item_id 는 row.item_id 에서 추출 (없으면 0). subscriber 가 payload.row 에서 다른 키도 읽음.
        """
        row = data.get("row") or {}
        try:
            item_id = int(row.get("item_id") or 0)
        except (TypeError, ValueError):
            item_id = 0
        return Event(
            event_type=EventType.DB_ROW_CHANGED,
            item_id=item_id,
            payload={
                "event": data.get("event"),
                "table": data.get("table"),
                "schema": data.get("schema"),
                "op": data.get("op"),
                "row": row,
                "old_row": data.get("old_row"),
                "at": data.get("at"),
            },
        )
