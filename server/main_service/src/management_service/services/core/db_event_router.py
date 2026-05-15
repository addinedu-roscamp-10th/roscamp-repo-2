"""DbEventRouter — DB_ROW_CHANGED event 를 in-process lifecycle 흐름으로 라우팅.

SPEC: docs/db_event_bridge/SPEC.md §6 Phase 3a (DB row trigger → backend 자동 진행)

흐름:
    [Phase 2] DB AFTER INSERT/UPDATE trigger → pg_notify → DbEventListener → publish(DB_ROW_CHANGED)
    [Phase 3a, 본 파일] DbEventRouter.subscribe(DB_ROW_CHANGED) → payload.table 별 handler
        - dual delivery 안전성: (table, row.PK, op) 기반 60s TTL dedup cache
        - handler 미등록 table: 무시 (silent)
    [Phase 3b, 별도 PR] task_executor 가 router 발행 in-process event 를 수신해 실 lifecycle 진입

Phase 3a 범위:
    - router 골격 + DB_ROW_CHANGED subscribe + payload.table 별 handler dispatch
    - 기본 handler: insp_task_txn / equip_task_txn / trans_task_txn / pp_task_txn / item / ord_stat
      → 현 단계 (Phase 3a) 는 logging 만 (실 lifecycle 진입은 Phase 3b 에서 task_executor 측 subscriber 추가)
    - dedup cache 로 dual delivery 안전성 미리 검증

설계 원칙:
    - subscriber 등록은 container 초기화 시점에 1회
    - dedup key 는 (table, row primary key, op) — handler 가 동일 row 이벤트 중복 처리 안 함
    - handler 예외 시 router 자체는 죽지 않음 (격리)
    - feature flag MGMT_DB_EVENT_BRIDGE_ENABLED 와 무관 — listener 가 publish 안 하면 router 도 호출 안 됨

@MX:NOTE: Phase 3a 골격 - logging 으로 라우팅 동작 검증. Phase 3b 에서 task_executor 측 실 처리 추가.
@MX:TODO: Phase 3b - task_executor.DB_ROW_CHANGED handler 추가 + insp_task_txn INSERT 에서 INSP 흐름 자동 진입
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from services.contracts.enums import EventType
from services.contracts.models import Event

if TYPE_CHECKING:
    from services.core.event_bridge import EventBridgeImpl

logger = logging.getLogger(__name__)

# dedup cache TTL — 동일 (table, PK, op) 이벤트가 짧은 시간 안에 중복으로 들어와도 한 번만 처리.
_DEDUP_TTL_SEC = 60.0

# table 별 primary key column 매핑 — dedup key 산출용.
# Phase 2 trigger 대상은 insp_task_txn 뿐이지만, Phase 3+ 확장 대비.
_TABLE_PK_COLUMN: dict[str, str] = {
    "insp_task_txn": "txn_id",
    "equip_task_txn": "txn_id",
    "trans_task_txn": "trans_task_txn_id",
    "pp_task_txn": "txn_id",
    "item": "item_id",
    "ord_stat": "stat_id",
}

# handler signature: (event: Event) -> None — 예외는 router 가 격리.
TableHandler = Callable[[Event], None]


class DbEventRouter:
    """DB_ROW_CHANGED event 를 payload.table 별 handler 로 라우팅.

    Container 초기화 시점에 인스턴스 생성 + register_handler() 로 table 별 핸들러 등록.
    EventBridge.subscribe() 호출은 attach(event_bridge) 시점에 1회.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[TableHandler]] = {}
        self._dedup_lock = threading.Lock()
        # dedup key (table:pk:op) → 처음 본 시각.
        self._dedup_seen: dict[str, float] = {}
        self._attached = False

    def register_handler(self, table: str, handler: TableHandler) -> None:
        """특정 table 의 DB_ROW_CHANGED event 에 호출될 handler 등록.

        같은 table 에 여러 handler 등록 가능 (실행 순서는 등록 순서).
        Phase 3a 는 logging handler 만 등록, Phase 3b 에서 task_executor 등 추가.
        """
        self._handlers.setdefault(table, []).append(handler)
        logger.info("DbEventRouter: handler 등록 table=%s total=%d", table, len(self._handlers[table]))

    def attach(self, event_bridge: "EventBridgeImpl") -> None:
        """EventBridge.subscribe(DB_ROW_CHANGED) — Container.start() 또는 __init__ 끝에서 1회."""
        if self._attached:
            logger.info("DbEventRouter: 이미 attached — skip")
            return
        event_bridge.subscribe(
            EventType.DB_ROW_CHANGED,
            self._on_db_row_changed,
            subscriber_name="db_event_router",
        )
        self._attached = True
        logger.info("DbEventRouter: attached to EventBridge (DB_ROW_CHANGED)")

    def _on_db_row_changed(self, event: Event) -> None:
        """DB_ROW_CHANGED subscriber callback — table 별 handler dispatch + dedup."""
        payload = event.payload or {}
        table = payload.get("table")
        op = payload.get("op")
        if not table or not op:
            logger.warning("DbEventRouter: payload 에 table/op 없음 — skip: %r", payload)
            return

        # dedup: 같은 (table, PK, op) 이벤트가 짧은 시간 안에 중복 들어와도 1회만 처리.
        if not self._mark_seen(table, op, payload):
            logger.info(
                "DbEventRouter: dedup hit table=%s op=%s — skip duplicate",
                table, op,
            )
            return

        handlers = self._handlers.get(table)
        if not handlers:
            logger.debug("DbEventRouter: no handlers for table=%s op=%s — skip", table, op)
            return

        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 — handler 격리 (다른 handler 영향 X)
                logger.warning(
                    "DbEventRouter: handler 예외 table=%s op=%s handler=%s exc=%s",
                    table, op, handler, exc,
                )

    def _mark_seen(self, table: str, op: str, payload: dict[str, Any]) -> bool:
        """dedup key 갱신. 처음 본 (table, PK, op) 면 True 반환, 중복이면 False."""
        pk_col = _TABLE_PK_COLUMN.get(table)
        row = payload.get("row") or {}
        pk_val = row.get(pk_col) if pk_col else None
        key = f"{table}:{pk_val}:{op}"
        now = time.time()
        with self._dedup_lock:
            seen_at = self._dedup_seen.get(key)
            if seen_at is not None and (now - seen_at) < _DEDUP_TTL_SEC:
                return False
            self._dedup_seen[key] = now
            # cache 크기 제한 — TTL 지난 entry 정리 (lazy GC, 매 100 entry 마다 1회).
            if len(self._dedup_seen) > 100 and len(self._dedup_seen) % 100 == 0:
                self._gc_expired_locked(now)
        return True

    def _gc_expired_locked(self, now: float) -> None:
        """dedup cache 의 TTL 지난 entry 제거 — 호출자가 lock 보유 가정."""
        expired = [k for k, t in self._dedup_seen.items() if (now - t) >= _DEDUP_TTL_SEC]
        for k in expired:
            del self._dedup_seen[k]
        if expired:
            logger.debug("DbEventRouter: gc dedup cache — removed %d expired entries", len(expired))


def make_logging_handler(table: str) -> TableHandler:
    """Phase 3a logging handler factory — payload 핵심 필드만 INFO 로 기록.

    실 lifecycle 진입은 Phase 3b 에서 task_executor 측 handler 가 담당.
    """

    def _handler(event: Event) -> None:
        payload = event.payload or {}
        row = payload.get("row") or {}
        op = payload.get("op")
        logger.info(
            "[db_event_router] table=%s op=%s item_id=%s row_pk=%s row_stat=%s",
            table,
            op,
            event.item_id,
            row.get(_TABLE_PK_COLUMN.get(table, "id")),
            row.get("txn_stat") or row.get("cur_stat") or row.get("ord_stat") or "-",
        )

    return _handler
