"""Phase 2 통합 테스트 — 실 DB trigger + 실 DbEventListener + EventBridge.publish 검증.

SPEC: docs/db_event_bridge/SPEC.md §6 Phase 2
선행: migrate_db_event_bridge.sql 적용 (`smartcast.notify_lifecycle_event` 함수 +
       insp_task_txn trigger 2종)

본 테스트는 실 DB 가 필요 — `DATABASE_URL` 미설정 시 skip.

흐름:
    1. DbEventListener 시작 (feature flag ON)
    2. 별도 psycopg connection 으로 INSERT INTO insp_task_txn
    3. listener 가 NOTIFY 수신 → EventBridge.publish(DB_ROW_CHANGED) 호출
    4. spy 가 published event 확인 (payload.table='insp_task_txn', row.txn_stat='PROC')
    5. cleanup — INSERT 한 row 삭제
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any

import pytest


@pytest.fixture()
def real_db_url() -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL 미설정 — Phase 2 통합 테스트 skip")
    return dsn


@pytest.fixture()
def event_bridge():
    """실 EventBridge — publish 만 검증 (subscriber 등록 없이 spy 로 기록)."""
    from services.core.event_bridge import EventBridgeImpl

    return EventBridgeImpl()


@pytest.fixture()
def captured_db_events(event_bridge):
    """DB_ROW_CHANGED subscriber spy — listener 가 relay 한 event 캡처."""
    from services.contracts.enums import EventType

    captured: list = []
    event_bridge.subscribe(
        EventType.DB_ROW_CHANGED,
        lambda evt: captured.append(evt),
        subscriber_name="phase2_test_spy",
    )
    return captured


def _ensure_item(dsn: str) -> int:
    """기존 item 중 하나의 item_id 를 가져옴 (테스트 INSERT 용)."""
    from sqlalchemy import create_engine, text

    eng = create_engine(dsn)
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT item_id FROM smartcast.item ORDER BY item_id DESC LIMIT 1")
        ).fetchone()
    eng.dispose()
    if row is None:
        pytest.skip("smartcast.item 행이 없음 — DB seed 필요")
    return int(row[0])


def _insert_proc_then_succ(dsn: str, item_id: int) -> int:
    """INSERT INTO insp_task_txn (PROC) → trigger 발동 → 그 후 UPDATE SUCC.

    Returns: 생성된 txn_id (cleanup 용)
    """
    from sqlalchemy import create_engine, text

    eng = create_engine(dsn)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with eng.connect() as conn:
        txn_id = conn.execute(
            text(
                "INSERT INTO smartcast.insp_task_txn (item_id, txn_stat, req_at, start_at) "
                "VALUES (:i, 'PROC', :n, :n) RETURNING txn_id"
            ),
            {"i": item_id, "n": now},
        ).scalar()
        conn.commit()
        # 즉시 SUCC 으로 UPDATE — UPDATE trigger 도 발동 검증
        conn.execute(
            text(
                "UPDATE smartcast.insp_task_txn "
                "SET txn_stat='SUCC', end_at=:n WHERE txn_id=:t"
            ),
            {"t": txn_id, "n": now},
        )
        conn.commit()
    eng.dispose()
    return int(txn_id)


def _cleanup_txn(dsn: str, txn_id: int) -> None:
    from sqlalchemy import create_engine, text

    eng = create_engine(dsn)
    with eng.connect() as conn:
        # final_inference_id FK 가 있으면 먼저 NULL 처리
        conn.execute(
            text(
                "UPDATE smartcast.insp_task_txn SET final_inference_id=NULL WHERE txn_id=:t"
            ),
            {"t": txn_id},
        )
        conn.execute(
            text("DELETE FROM smartcast.insp_task_txn WHERE txn_id=:t"),
            {"t": txn_id},
        )
        conn.commit()
    eng.dispose()


def test_insp_task_txn_trigger_publishes_db_row_changed(
    real_db_url: str,
    event_bridge,
    captured_db_events: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INSERT INTO insp_task_txn (PROC) → trigger → NOTIFY → listener → EventBridge.publish 검증."""
    monkeypatch.setenv("MGMT_DB_EVENT_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("MGMT_DB_EVENT_CHANNEL", "lifecycle_event")

    from services.core.db_event_listener import DbEventListener

    listener = DbEventListener(event_bridge=event_bridge, dsn=real_db_url)

    async def run_test() -> int:
        await listener.start()
        # listener 가 LISTEN 등록될 때까지 짧게 대기
        await asyncio.sleep(0.5)
        try:
            item_id = _ensure_item(real_db_url)
            txn_id = _insert_proc_then_succ(real_db_url, item_id)
            # NOTIFY 가 도달할 시간 충분히 대기
            for _ in range(20):
                if len(captured_db_events) >= 2:
                    break
                await asyncio.sleep(0.1)
            return txn_id
        finally:
            await listener.stop()

    txn_id = asyncio.run(run_test())

    try:
        # INSERT + UPDATE 각 1회씩 → 최소 2건 event
        assert len(captured_db_events) >= 2, (
            f"expected >=2 events, got {len(captured_db_events)}: {captured_db_events}"
        )

        insert_evt = next(
            (e for e in captured_db_events if (e.payload or {}).get("op") == "INSERT"),
            None,
        )
        update_evt = next(
            (e for e in captured_db_events if (e.payload or {}).get("op") == "UPDATE"),
            None,
        )
        assert insert_evt is not None, "INSERT event 미발생"
        assert update_evt is not None, "UPDATE event 미발생"

        # payload 검증
        insert_payload = insert_evt.payload
        assert insert_payload["table"] == "insp_task_txn"
        assert insert_payload["op"] == "INSERT"
        assert insert_payload["row"]["txn_id"] == txn_id
        assert insert_payload["row"]["txn_stat"] == "PROC"
        assert insert_payload["old_row"] is None

        update_payload = update_evt.payload
        assert update_payload["table"] == "insp_task_txn"
        assert update_payload["op"] == "UPDATE"
        assert update_payload["row"]["txn_id"] == txn_id
        assert update_payload["row"]["txn_stat"] == "SUCC"
        assert update_payload["old_row"] is not None
        assert update_payload["old_row"]["txn_stat"] == "PROC"

        # EventType.DB_ROW_CHANGED 정합
        from services.contracts.enums import EventType
        assert insert_evt.event_type == EventType.DB_ROW_CHANGED
    finally:
        _cleanup_txn(real_db_url, txn_id)
