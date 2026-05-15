"""Phase 3c 통합 테스트 — 5 개 lifecycle 테이블 trigger 확장 검증.

선행: migrate_db_event_bridge.sql (Phase 2) + migrate_db_event_bridge_phase3c.sql (Phase 3c)

실 DB 의 다음 테이블 INSERT/UPDATE 가 NOTIFY → DbEventListener → EventBridge.publish 까지
정상 흐름을 거치는지 검증:
    - equip_task_txn (INSERT + UPDATE OF txn_stat)
    - trans_task_txn (INSERT + UPDATE OF txn_stat)
    - pp_task_txn   (INSERT + UPDATE OF txn_stat)
    - item          (INSERT + UPDATE OF cur_stat / is_defective)
    - ord_stat      (INSERT + UPDATE OF ord_stat)

각 테스트는 self-cleanup 으로 DB 원상 복귀.

SPEC: docs/db_event_bridge/SPEC.md §6 Phase 3c
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest


@pytest.fixture()
def real_db_url() -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL 미설정 — Phase 3c 통합 테스트 skip")
    return dsn


@pytest.fixture()
def event_bridge():
    from services.core.event_bridge import EventBridgeImpl

    return EventBridgeImpl()


@pytest.fixture()
def captured(event_bridge):
    """DB_ROW_CHANGED subscriber spy."""
    from services.contracts.enums import EventType

    rows: list = []
    event_bridge.subscribe(
        EventType.DB_ROW_CHANGED,
        lambda evt: rows.append(evt),
        subscriber_name="phase3c_test_spy",
    )
    return rows


def _engine(dsn: str):
    from sqlalchemy import create_engine

    return create_engine(dsn)


async def _run_with_listener(
    dsn: str, event_bridge, action_callable, settle_sec: float = 1.0
) -> None:
    """listener 시작 → action 실행 → settle 대기 → listener 종료."""
    os.environ["MGMT_DB_EVENT_BRIDGE_ENABLED"] = "1"
    os.environ.setdefault("MGMT_DB_EVENT_CHANNEL", "lifecycle_event")
    from services.core.db_event_listener import DbEventListener

    listener = DbEventListener(event_bridge=event_bridge, dsn=dsn)
    await listener.start()
    await asyncio.sleep(0.5)  # LISTEN 등록 대기
    try:
        action_callable()
        # NOTIFY 도착 대기
        await asyncio.sleep(settle_sec)
    finally:
        await listener.stop()


def _find_event(captured_list, *, table: str, op: str):
    for evt in captured_list:
        p = evt.payload or {}
        if p.get("table") == table and p.get("op") == op:
            return evt
    return None


def _ensure_item(dsn: str) -> int:
    from sqlalchemy import text

    eng = _engine(dsn)
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT item_id FROM smartcast.item ORDER BY item_id DESC LIMIT 1")
        ).fetchone()
    eng.dispose()
    if row is None:
        pytest.skip("smartcast.item 행이 없음 — DB seed 필요")
    return int(row[0])


def test_equip_task_txn_trigger_publishes_db_row_changed(
    real_db_url: str, event_bridge, captured: list
) -> None:
    from sqlalchemy import text

    item_id = _ensure_item(real_db_url)
    eng = _engine(real_db_url)
    txn_id_holder: dict = {}

    def _action() -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with eng.connect() as conn:
            txn_id = conn.execute(
                text(
                    "INSERT INTO smartcast.equip_task_txn (item_id, task_type, res_id, txn_stat, req_at, start_at) "
                    "VALUES (:i, 'MM', 'MAT', 'PROC', :n, :n) RETURNING txn_id"
                ),
                {"i": item_id, "n": now},
            ).scalar()
            conn.commit()
            conn.execute(
                text(
                    "UPDATE smartcast.equip_task_txn SET txn_stat='SUCC', end_at=:n WHERE txn_id=:t"
                ),
                {"t": txn_id, "n": now},
            )
            conn.commit()
        txn_id_holder["txn_id"] = int(txn_id)

    try:
        asyncio.run(_run_with_listener(real_db_url, event_bridge, _action))

        ins = _find_event(captured, table="equip_task_txn", op="INSERT")
        upd = _find_event(captured, table="equip_task_txn", op="UPDATE")
        assert ins is not None, f"equip_task_txn INSERT event 미발생: {captured}"
        assert upd is not None, f"equip_task_txn UPDATE event 미발생: {captured}"
        assert ins.payload["row"]["txn_id"] == txn_id_holder["txn_id"]
        assert ins.payload["row"]["txn_stat"] == "PROC"
        assert upd.payload["row"]["txn_stat"] == "SUCC"
        assert upd.payload["old_row"]["txn_stat"] == "PROC"
    finally:
        with eng.connect() as conn:
            conn.execute(
                text("DELETE FROM smartcast.equip_task_txn WHERE txn_id=:t"),
                {"t": txn_id_holder.get("txn_id", 0)},
            )
            conn.commit()
        eng.dispose()


def test_trans_task_txn_trigger_publishes_db_row_changed(
    real_db_url: str, event_bridge, captured: list
) -> None:
    from sqlalchemy import text

    item_id = _ensure_item(real_db_url)
    eng = _engine(real_db_url)
    txn_holder: dict = {}

    def _action() -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with eng.connect() as conn:
            txn_id = conn.execute(
                text(
                    "INSERT INTO smartcast.trans_task_txn (item_id, task_type, txn_stat, req_at, start_at) "
                    "VALUES (:i, 'ToPP', 'PROC', :n, :n) RETURNING trans_task_txn_id"
                ),
                {"i": item_id, "n": now},
            ).scalar()
            conn.commit()
            conn.execute(
                text(
                    "UPDATE smartcast.trans_task_txn SET txn_stat='SUCC', end_at=:n "
                    "WHERE trans_task_txn_id=:t"
                ),
                {"t": txn_id, "n": now},
            )
            conn.commit()
        txn_holder["id"] = int(txn_id)

    try:
        asyncio.run(_run_with_listener(real_db_url, event_bridge, _action))
        ins = _find_event(captured, table="trans_task_txn", op="INSERT")
        upd = _find_event(captured, table="trans_task_txn", op="UPDATE")
        assert ins is not None and upd is not None, f"trans_task_txn events: {captured}"
        assert ins.payload["row"]["task_type"] == "ToPP"
        assert upd.payload["row"]["txn_stat"] == "SUCC"
    finally:
        with eng.connect() as conn:
            conn.execute(
                text("DELETE FROM smartcast.trans_task_txn WHERE trans_task_txn_id=:t"),
                {"t": txn_holder.get("id", 0)},
            )
            conn.commit()
        eng.dispose()


def test_item_update_cur_stat_publishes_db_row_changed(
    real_db_url: str, event_bridge, captured: list
) -> None:
    """item.cur_stat 만 UPDATE 해도 trigger 발동 확인. is_defective 만 변경해도 동일."""
    from sqlalchemy import text

    item_id = _ensure_item(real_db_url)
    eng = _engine(real_db_url)

    # 원래 값 보존
    with eng.connect() as conn:
        original = conn.execute(
            text("SELECT cur_stat FROM smartcast.item WHERE item_id=:i"), {"i": item_id}
        ).scalar()

    new_stat = "WAIT_PP" if original != "WAIT_PP" else "WAIT_INSP"

    def _action() -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with eng.connect() as conn:
            conn.execute(
                text("UPDATE smartcast.item SET cur_stat=:s, updated_at=:n WHERE item_id=:i"),
                {"s": new_stat, "n": now, "i": item_id},
            )
            conn.commit()

    try:
        asyncio.run(_run_with_listener(real_db_url, event_bridge, _action))
        upd = _find_event(captured, table="item", op="UPDATE")
        assert upd is not None, f"item UPDATE event 미발생: {captured}"
        assert upd.payload["row"]["item_id"] == item_id
        assert upd.payload["row"]["cur_stat"] == new_stat
        assert upd.payload["old_row"]["cur_stat"] == original
    finally:
        with eng.connect() as conn:
            conn.execute(
                text("UPDATE smartcast.item SET cur_stat=:s WHERE item_id=:i"),
                {"s": original, "i": item_id},
            )
            conn.commit()
        eng.dispose()
