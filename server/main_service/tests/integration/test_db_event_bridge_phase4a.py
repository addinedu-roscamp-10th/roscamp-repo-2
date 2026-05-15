"""Phase 4a-2 통합 검증 — DB UPDATE → TASK_COMPLETED publish + dual delivery 안전.

SPEC: docs/db_event_bridge/SPEC.md §6 Phase 4 + PHASE4_ANALYSIS.md
선행 PR: #28~#34

시나리오:
    1. 외부 SQL UPDATE INTO equip_task_txn SET txn_stat='SUCC'
       → DB trigger → NOTIFY → DbEventListener → DbEventRouter
       → TaskTxnUpdateDispatcher → EventBridge.publish(TASK_COMPLETED)
       → test subscriber 가 (item_id, task_type) waiter 해제
    2. dual delivery: state_manager 시뮬레이션 publish + DB-origin publish 둘 다 발생
       → waiter 가 한 번만 resolve (pop list 자연 dedup)

실 DB 필요 — `DATABASE_URL` 미설정 시 skip.
선행 마이그레이션: migrate_db_event_bridge.sql + migrate_db_event_bridge_phase3c.sql 적용 필수.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import pytest


@pytest.fixture()
def real_db_url() -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL 미설정")
    return dsn


def _ensure_item(dsn: str) -> int:
    from sqlalchemy import create_engine, text

    eng = create_engine(dsn)
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT item_id FROM inbean.item ORDER BY item_id DESC LIMIT 1")
        ).fetchone()
    eng.dispose()
    if row is None:
        # smartcast 도 시도
        eng = create_engine(dsn)
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT item_id FROM smartcast.item ORDER BY item_id DESC LIMIT 1")
            ).fetchone()
        eng.dispose()
    if row is None:
        pytest.skip("item 행 없음")
    return int(row[0])


def _cleanup_txn(dsn: str, txn_id: int, schema: str) -> None:
    from sqlalchemy import create_engine, text

    eng = create_engine(dsn)
    with eng.connect() as conn:
        conn.execute(
            text(f"DELETE FROM {schema}.equip_task_txn WHERE txn_id=:t"),
            {"t": txn_id},
        )
        conn.commit()
    eng.dispose()


def test_sql_update_triggers_task_completed_publish(
    real_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """외부 SQL UPDATE INTO equip_task_txn (SUCC) → TASK_COMPLETED publish 검증."""
    monkeypatch.setenv("MGMT_DB_EVENT_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    monkeypatch.setenv("MGMT_DB_EVENT_CHANNEL", "lifecycle_event")

    from services.contracts.enums import EventType, TaskType
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher
    from services.core.db_event_listener import DbEventListener
    from services.core.db_event_router import DbEventRouter
    from services.core.event_bridge import EventBridgeImpl

    # SCHEMA 결정 — backend 가 현재 사용하는 schema
    schema = os.environ.get("SMARTCAST_SCHEMA", "smartcast")
    item_id = _ensure_item(real_db_url)

    # in-process 컴포넌트 구성
    bridge = EventBridgeImpl()
    dispatcher = TaskTxnUpdateDispatcher(event_bridge=bridge)
    router = DbEventRouter()
    router.register_handler("equip_task_txn", dispatcher.handle)
    router.attach(bridge)
    listener = DbEventListener(event_bridge=bridge, dsn=real_db_url)

    # TASK_COMPLETED 캡처 spy + waiter
    captured: list = []
    bridge.subscribe(
        EventType.TASK_COMPLETED,
        lambda evt: captured.append(evt),
        subscriber_name="phase4a_test_spy",
    )

    # background loop (listener 운영용)
    loop = asyncio.new_event_loop()
    loop_started = threading.Event()

    def _run_loop():
        asyncio.set_event_loop(loop)
        loop_started.set()
        loop.run_forever()

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()
    loop_started.wait(timeout=2.0)

    inserted_txn_id: int | None = None
    try:
        # listener 시작
        asyncio.run_coroutine_threadsafe(listener.start(), loop).result(timeout=5.0)
        time.sleep(0.5)  # LISTEN 등록 대기

        # 외부 SQL: PROC INSERT + SUCC UPDATE
        from sqlalchemy import create_engine, text

        eng = create_engine(real_db_url)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with eng.connect() as conn:
            inserted_txn_id = conn.execute(
                text(
                    f"INSERT INTO {schema}.equip_task_txn "
                    f"(item_id, task_type, res_id, txn_stat, req_at, start_at) "
                    f"VALUES (:i, 'MM', 'MAT', 'PROC', :n, :n) RETURNING txn_id"
                ),
                {"i": item_id, "n": now},
            ).scalar()
            conn.commit()
            conn.execute(
                text(
                    f"UPDATE {schema}.equip_task_txn SET txn_stat='SUCC', end_at=:n "
                    f"WHERE txn_id=:t"
                ),
                {"t": inserted_txn_id, "n": now},
            )
            conn.commit()
        eng.dispose()
        inserted_txn_id = int(inserted_txn_id)

        # NOTIFY 도착 + publish 대기
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if any(
                e.payload.get("task_type") == TaskType.MM
                and e.payload.get("status") == "SUCC"
                and e.item_id == item_id
                for e in captured
            ):
                break
            time.sleep(0.1)

        # 정합 검증
        mm_succ_events = [
            e
            for e in captured
            if e.payload.get("task_type") == TaskType.MM
            and e.payload.get("status") == "SUCC"
            and e.item_id == item_id
        ]
        assert len(mm_succ_events) >= 1, (
            f"TASK_COMPLETED(MM, SUCC) publish 안 됨. captured={captured}"
        )
        evt = mm_succ_events[0]
        assert evt.event_type == EventType.TASK_COMPLETED
        assert evt.payload["_origin"] == "db_event_router"
        assert evt.payload["_table"] == "equip_task_txn"
        assert evt.payload["_txn_id"] == inserted_txn_id

    finally:
        try:
            asyncio.run_coroutine_threadsafe(listener.stop(), loop).result(timeout=5.0)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=3.0)
        if not loop.is_closed():
            loop.close()
        if inserted_txn_id is not None:
            _cleanup_txn(real_db_url, inserted_txn_id, schema)


def test_dual_delivery_waiter_resolves_only_once() -> None:
    """state_manager publish + DB-origin publish 동시 도착 → waiter 1회만 resolve.

    task_executor._task_waiters.pop() list 의 자연 dedup 동작 검증.
    실 DB 없이 in-process EventBridge + 시뮬레이션 publish 둘로 검증.
    """
    from services.contracts.enums import EventType, TaskType, TxnStat
    from services.contracts.models import Event
    from services.core.event_bridge import EventBridgeImpl

    bridge = EventBridgeImpl()

    # task_executor 의 _task_waiters 패턴 시뮬레이션
    waiters: dict[tuple[int, TaskType], list[asyncio.Future]] = defaultdict(list)
    resolve_count = [0]
    invalid_state_count = [0]

    def on_task_completed(event: Event) -> None:
        item_id = event.item_id
        task_type = event.payload.get("task_type")
        status = event.payload.get("status", TxnStat.SUCC.value)
        if item_id is None or not isinstance(task_type, TaskType):
            return
        key = (item_id, task_type)
        # task_executor 와 동일하게 pop → 자연 dedup
        futures = waiters.pop(key, [])
        for f in futures:
            if not f.done():
                f.set_result(status)
                resolve_count[0] += 1
            else:
                invalid_state_count[0] += 1

    bridge.subscribe(EventType.TASK_COMPLETED, on_task_completed, "test_waiter")

    # waiter 1개 등록
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    future: asyncio.Future = loop.create_future()
    waiters[(29, TaskType.MM)].append(future)

    # dual publish: state_manager 시뮬레이션 + DB-origin 시뮬레이션
    state_manager_event = Event(
        event_type=EventType.TASK_COMPLETED,
        item_id=29,
        payload={"task_type": TaskType.MM, "status": "SUCC", "_origin": "state_manager"},
    )
    db_origin_event = Event(
        event_type=EventType.TASK_COMPLETED,
        item_id=29,
        payload={
            "task_type": TaskType.MM,
            "status": "SUCC",
            "_origin": "db_event_router",
        },
    )

    bridge.publish(state_manager_event)
    bridge.publish(db_origin_event)

    # 검증: future 한 번만 resolve, InvalidStateError 안 발생
    assert future.done()
    assert future.result() == "SUCC"
    assert resolve_count[0] == 1, f"resolve 한 번만 일어나야 함: {resolve_count[0]}"
    assert invalid_state_count[0] == 0, (
        f"이미 done 된 future set 시도 안 일어나야 함: {invalid_state_count[0]}"
    )
    # 두 번째 publish 시 waiters[key] 가 이미 pop 되어 빈 list — 호출 안 됨
    assert waiters.get((29, TaskType.MM), []) == []

    loop.close()
