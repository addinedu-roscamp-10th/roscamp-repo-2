"""DB row trigger → task lifecycle 자동 진행 dispatcher 모음 (Phase 3b).

SPEC: docs/db_event_bridge/SPEC.md §6 Phase 3b

본 모듈은 `DbEventRouter` 에 등록되어 동작하며, 외부 SQL UPDATE 만으로
backend 의 lifecycle 이 진행되도록 한다.

설계 원칙:
    - feature flag `MGMT_DB_EVENT_TASK_DISPATCH=1` 일 때만 활성 (기본 off → 머지 후 동작 변경 없음)
    - dual delivery 안전성:
        a. DbEventRouter 의 (table, PK, op) 60s dedup cache 가 1차 차단
        b. state_manager.consume_inspection_image 가 1회 소비 후 clear — in-process flow
           가 이미 처리했으면 dispatcher 는 silent skip (자연 dedup)
    - handler 예외는 router 가 격리 — 한 dispatcher 실패가 다른 dispatcher 영향 X
    - AIAdapter (sync) + state_manager.record_inspection_result (async) 둘 다 호출
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from services.contracts.enums import EventType, TaskType, TxnStat
from services.contracts.models import Event

if TYPE_CHECKING:
    from services.core.adapters.ai_adapter import AIAdapter
    from services.core.event_bridge import EventBridgeImpl
    from services.core.state_manager import StateManager

logger = logging.getLogger(__name__)


# Phase 4a: task_txn UPDATE → TASK_COMPLETED 처리 대상 테이블
_TRACKED_TASK_TXN_TABLES: frozenset[str] = frozenset({
    "equip_task_txn", "trans_task_txn", "pp_task_txn", "insp_task_txn",
})
# task lifecycle 종결 상태 (PROC/QUE 는 신호 아님)
_TERMINAL_TASK_STATS: frozenset[str] = frozenset({
    TxnStat.SUCC.value, TxnStat.FAIL.value,
})
# table 별 고정 TaskType (row.task_type 컬럼 없는 테이블용)
_TABLE_FIXED_TASK_TYPE: dict[str, TaskType] = {
    "insp_task_txn": TaskType.INSP,
    "pp_task_txn": TaskType.PP,
}


class InspTaskTxnDispatcher:
    """insp_task_txn INSERT(PROC) → 자동 AIAdapter + DB 영속화.

    동작:
        1. event.payload.op='INSERT' + row.txn_stat='PROC' 만 처리 (UPDATE 는 Phase 3c)
        2. state_manager.consume_inspection_image(item_id) 로 image_path 조회
           → None 이면 in-process flow 가 이미 소비한 것 (또는 image 미수신) → silent skip
        3. AIAdapter.execute(item_id, payload={image_path}) — sync 호출
        4. state_manager.record_inspection_result(item_id, inference) — async, run_coroutine_threadsafe

    External SQL INSERT 시나리오에서 동작하려면 호출 직전에
    state_manager.update_inspection_image() 로 image registry 가 채워져 있어야 함.
    정상 운영 흐름에서는 INSP_IMAGE_UPLOADED 가 이미 채워둠.
    """

    def __init__(
        self,
        *,
        adapter: "AIAdapter",
        state_manager: "StateManager",
    ) -> None:
        self._adapter = adapter
        self._state_manager = state_manager
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _is_enabled() -> bool:
        return os.environ.get("MGMT_DB_EVENT_TASK_DISPATCH", "0") in ("1", "true", "yes")

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """async record_inspection_result 호출용 loop 주입.

        server.py 가 OrchestratorThread 생성 직후 1회 호출.
        """
        self._loop = loop

    def handle(self, event: "Event") -> None:
        """DbEventRouter 의 insp_task_txn handler — DB_ROW_CHANGED 수신 시 호출."""
        if not self._is_enabled():
            return
        payload = event.payload or {}
        op = payload.get("op")
        row = payload.get("row") or {}

        if op != "INSERT":
            return
        if row.get("txn_stat") != "PROC":
            return

        item_id = int(row.get("item_id") or 0)
        if item_id <= 0:
            logger.info("[InspDispatcher] item_id 없음 — skip")
            return
        txn_id = row.get("txn_id")

        image_meta = self._state_manager.consume_inspection_image(item_id)
        if image_meta is None:
            logger.info(
                "[InspDispatcher] item_id=%s insp_txn=%s — image registry 비어있음 "
                "(in-process flow 가 이미 소비했거나 image 미수신) → skip",
                item_id, txn_id,
            )
            return

        image_path = image_meta.get("image_path")
        if not image_path or not Path(image_path).exists():
            logger.warning(
                "[InspDispatcher] item_id=%s image_path 없음/미존재: %r",
                item_id, image_path,
            )
            return

        # AIAdapter.execute — sync
        adapter_payload = json.dumps({"image_path": str(image_path)}).encode("utf-8")
        try:
            result = self._adapter.execute(
                item_id=item_id,
                _robot_id="AI",
                _command="AI_INFERENCE_REQUEST",
                payload=adapter_payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[InspDispatcher] AIAdapter.execute 예외 item_id=%s insp_txn=%s exc=%s",
                item_id, txn_id, exc,
            )
            return

        inference = (result.payload or {}).get("inference") or {}

        # state_manager.record_inspection_result — async, run_coroutine_threadsafe
        if self._loop is None:
            logger.warning(
                "[InspDispatcher] loop 미설정 — record_inspection_result skip "
                "(server.py 가 set_loop 호출했는지 확인)"
            )
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._state_manager.record_inspection_result(
                    item_id=item_id, inference=inference
                ),
                self._loop,
            )
            ok = future.result(timeout=10.0)
            logger.info(
                "[InspDispatcher] dispatched item_id=%s insp_txn=%s "
                "AI.success=%s record=%s",
                item_id, txn_id, result.success, ok,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[InspDispatcher] record_inspection_result 예외 item_id=%s insp_txn=%s exc=%s",
                item_id, txn_id, exc,
            )


class TaskTxnUpdateDispatcher:
    """task_txn UPDATE(SUCC/FAIL) → in-process TASK_COMPLETED publish (Phase 4a).

    SPEC: docs/db_event_bridge/SPEC.md §6 Phase 4 + PHASE4_ANALYSIS.md §3.1

    동작:
        DB AFTER UPDATE OF txn_stat trigger → pg_notify → router → 본 dispatcher
        → EventBridge.publish(TASK_COMPLETED, payload={task_type, status})
        → task_executor._on_task_completed (기존 subscriber) 가 동일하게 waiter 해제

    dual delivery 안전성:
        - DbEventRouter 가 (table, PK, op) 60s dedup 으로 DB-origin 중복 차단
        - state_manager 가 in-memory TASK_COMPLETED 도 발행 (기존) → 둘이 동일 (item_id, task_type)
          publish 하지만 task_executor._on_task_completed 가 _task_waiters.pop() 사용 (list).
          한 번 pop 되면 두 번째는 empty list → 두 번째 publish 는 no-op (안전).
        - 즉 in-memory publish 가 먼저 도착하든 DB-origin 이 먼저 도착하든 한 번만 처리됨.
        - Phase 4a 의 다음 단계 (별도 PR) 에서 state_manager 의 in-memory publish 를 제거하면
          오직 DB-origin 만 신호 → DB 가 단일 진실 원천.

    table 별 task_type 결정:
        - equip_task_txn / trans_task_txn: row.task_type (예: 'MM', 'POUR', 'DM', 'ToPP')
        - pp_task_txn: 고정 TaskType.PP (row 에 task_type 컬럼 없음)
        - insp_task_txn: 고정 TaskType.INSP (row 에 task_type 컬럼 없음)

    feature flag: MGMT_DB_EVENT_TASK_DISPATCH=1 일 때만 활성 (기본 off).
    """

    def __init__(self, *, event_bridge: "EventBridgeImpl") -> None:
        self._event_bridge = event_bridge

    @staticmethod
    def _is_enabled() -> bool:
        return os.environ.get("MGMT_DB_EVENT_TASK_DISPATCH", "0") in ("1", "true", "yes")

    def handle(self, event: Event) -> None:
        """DbEventRouter 의 task_txn handler — UPDATE OF txn_stat 만 처리."""
        if not self._is_enabled():
            return
        payload = event.payload or {}
        op = payload.get("op")
        if op != "UPDATE":
            return
        table = payload.get("table")
        if table not in _TRACKED_TASK_TXN_TABLES:
            return

        row = payload.get("row") or {}
        new_stat = row.get("txn_stat")
        if new_stat not in _TERMINAL_TASK_STATS:
            return  # PROC/QUE 등은 종결 신호 아님

        old_row = payload.get("old_row") or {}
        if old_row.get("txn_stat") == new_stat:
            return  # 의미 있는 전이 아님 (다른 컬럼만 변경)

        item_id_raw = row.get("item_id")
        if item_id_raw is None:
            return
        try:
            item_id = int(item_id_raw)
        except (TypeError, ValueError):
            return

        # table 별 task_type 결정
        fixed = _TABLE_FIXED_TASK_TYPE.get(table)
        if fixed is not None:
            task_type = fixed
        else:
            raw = row.get("task_type")
            try:
                task_type = TaskType(raw)
            except (TypeError, ValueError):
                logger.info(
                    "[TaskTxnDispatcher] unknown task_type=%r table=%s — skip",
                    raw, table,
                )
                return

        try:
            self._event_bridge.publish(
                Event(
                    event_type=EventType.TASK_COMPLETED,
                    item_id=item_id,
                    payload={
                        "task_type": task_type,
                        "status": new_stat,
                        "_origin": "db_event_router",
                        "_table": table,
                        "_txn_id": row.get("txn_id") or row.get("trans_task_txn_id"),
                    },
                )
            )
            logger.info(
                "[TaskTxnDispatcher] publish TASK_COMPLETED item_id=%s task_type=%s "
                "status=%s table=%s",
                item_id, task_type.value, new_stat, table,
            )
        except Exception as exc:  # noqa: BLE001 — handler 격리
            logger.warning(
                "[TaskTxnDispatcher] publish 실패 item_id=%s table=%s exc=%s",
                item_id, table, exc,
            )
