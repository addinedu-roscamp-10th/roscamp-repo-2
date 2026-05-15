"""DbEventRouter (Phase 3a) 단위 테스트.

라우팅, dedup, handler 격리 동작을 EventBridge 없이 직접 검증.
SPEC: docs/db_event_bridge/SPEC.md §6 Phase 3a
"""

from __future__ import annotations

import time

import pytest


def _make_event(table: str, op: str, row: dict, item_id: int = 0):
    from services.contracts.enums import EventType
    from services.contracts.models import Event

    return Event(
        event_type=EventType.DB_ROW_CHANGED,
        item_id=item_id,
        payload={
            "event": f"{table}_{op.lower()}",
            "table": table,
            "op": op,
            "row": row,
            "old_row": None,
            "at": "2026-05-15T09:18:23.731Z",
        },
    )


def test_router_dispatches_to_registered_handler() -> None:
    from services.core.db_event_router import DbEventRouter

    router = DbEventRouter()
    seen: list = []
    router.register_handler("insp_task_txn", lambda evt: seen.append(evt))

    evt = _make_event("insp_task_txn", "INSERT", {"txn_id": 100, "txn_stat": "PROC"})
    router._on_db_row_changed(evt)
    assert len(seen) == 1
    assert seen[0].payload["op"] == "INSERT"


def test_router_silently_skips_unregistered_table() -> None:
    from services.core.db_event_router import DbEventRouter

    router = DbEventRouter()
    seen: list = []
    router.register_handler("insp_task_txn", lambda evt: seen.append(evt))

    # 다른 table 이벤트 → router 가 silent skip (router 자체는 죽지 않음)
    evt = _make_event("ai_inference_txn", "INSERT", {"inference_id": 1})
    router._on_db_row_changed(evt)
    assert seen == []


def test_router_dedup_blocks_duplicate_same_row_op() -> None:
    from services.core.db_event_router import DbEventRouter

    router = DbEventRouter()
    seen: list = []
    router.register_handler("insp_task_txn", lambda evt: seen.append(evt))

    # 동일 (table, txn_id, op) 2회 → 첫 회만 처리
    evt = _make_event("insp_task_txn", "INSERT", {"txn_id": 200, "txn_stat": "PROC"})
    router._on_db_row_changed(evt)
    router._on_db_row_changed(evt)
    assert len(seen) == 1


def test_router_different_op_same_row_allowed() -> None:
    """같은 row 라도 INSERT 와 UPDATE 는 별개 dedup key — 둘 다 처리."""
    from services.core.db_event_router import DbEventRouter

    router = DbEventRouter()
    seen: list = []
    router.register_handler("insp_task_txn", lambda evt: seen.append(evt))

    insert_evt = _make_event("insp_task_txn", "INSERT", {"txn_id": 300, "txn_stat": "PROC"})
    update_evt = _make_event("insp_task_txn", "UPDATE", {"txn_id": 300, "txn_stat": "SUCC"})
    router._on_db_row_changed(insert_evt)
    router._on_db_row_changed(update_evt)
    assert len(seen) == 2
    assert seen[0].payload["op"] == "INSERT"
    assert seen[1].payload["op"] == "UPDATE"


def test_router_handler_exception_does_not_crash_router() -> None:
    from services.core.db_event_router import DbEventRouter

    router = DbEventRouter()

    def crashing(_evt):
        raise RuntimeError("boom")

    seen: list = []
    router.register_handler("insp_task_txn", crashing)
    router.register_handler("insp_task_txn", lambda evt: seen.append(evt))

    evt = _make_event("insp_task_txn", "INSERT", {"txn_id": 400, "txn_stat": "PROC"})
    # 첫 handler 예외 → 두 번째 handler 는 정상 실행
    router._on_db_row_changed(evt)
    assert len(seen) == 1


def test_router_missing_table_or_op_skip() -> None:
    from services.contracts.enums import EventType
    from services.contracts.models import Event
    from services.core.db_event_router import DbEventRouter

    router = DbEventRouter()
    seen: list = []
    router.register_handler("insp_task_txn", lambda evt: seen.append(evt))

    bad_evt = Event(
        event_type=EventType.DB_ROW_CHANGED,
        item_id=0,
        payload={"row": {"x": 1}},  # table/op 누락
    )
    router._on_db_row_changed(bad_evt)
    assert seen == []


def test_make_logging_handler_returns_callable() -> None:
    from services.core.db_event_router import make_logging_handler

    h = make_logging_handler("insp_task_txn")
    assert callable(h)
    # 호출 시 예외 없이 통과 (logger.info 만 호출)
    h(_make_event("insp_task_txn", "INSERT", {"txn_id": 500, "txn_stat": "PROC"}))


def test_router_attach_idempotent() -> None:
    from services.core.db_event_router import DbEventRouter
    from unittest.mock import MagicMock

    router = DbEventRouter()
    bridge = MagicMock()
    router.attach(bridge)
    router.attach(bridge)  # 2회 호출 → 1회만 subscribe
    assert bridge.subscribe.call_count == 1
