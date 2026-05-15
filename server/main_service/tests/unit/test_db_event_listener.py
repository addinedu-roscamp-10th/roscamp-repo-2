"""DbEventListener (Phase 1 골격) 단위 테스트.

asyncpg 실 connection 없이 listener callback / event 변환 / feature flag 동작을 검증.
SPEC: docs/db_event_bridge/SPEC.md
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def event_bridge_spy():
    """EventBridge spy — publish() 호출만 기록."""
    bridge = MagicMock()
    bridge.published: list = []
    bridge.publish.side_effect = lambda evt: bridge.published.append(evt)
    return bridge


def test_listener_feature_flag_off_skips_start(monkeypatch, event_bridge_spy) -> None:
    monkeypatch.delenv("MGMT_DB_EVENT_BRIDGE_ENABLED", raising=False)
    from services.core.db_event_listener import DbEventListener

    listener = DbEventListener(event_bridge=event_bridge_spy, dsn="postgresql://localhost/test")
    import asyncio

    asyncio.run(listener.start())
    assert listener._task is None  # feature flag off → start no-op


def test_listener_missing_dsn_skips_start(monkeypatch, event_bridge_spy) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_BRIDGE_ENABLED", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from services.core.db_event_listener import DbEventListener

    listener = DbEventListener(event_bridge=event_bridge_spy, dsn="")
    import asyncio

    asyncio.run(listener.start())
    assert listener._task is None


def test_build_event_normalizes_payload(event_bridge_spy) -> None:
    from services.contracts.enums import EventType
    from services.core.db_event_listener import DbEventListener

    listener = DbEventListener(event_bridge=event_bridge_spy)
    data: dict[str, Any] = {
        "event": "insp_task_txn_insert",
        "schema": "smartcast",
        "table": "insp_task_txn",
        "op": "INSERT",
        "row": {"txn_id": 14, "item_id": 29, "txn_stat": "PROC"},
        "old_row": None,
        "at": "2026-05-15T09:18:23.731Z",
    }
    event = listener._build_event(data)
    assert event.event_type == EventType.DB_ROW_CHANGED
    assert event.item_id == 29
    assert event.payload["table"] == "insp_task_txn"
    assert event.payload["op"] == "INSERT"
    assert event.payload["row"]["txn_stat"] == "PROC"
    assert event.payload["at"] == "2026-05-15T09:18:23.731Z"


def test_build_event_missing_item_id_defaults_to_zero(event_bridge_spy) -> None:
    from services.core.db_event_listener import DbEventListener

    listener = DbEventListener(event_bridge=event_bridge_spy)
    data = {"table": "ord_stat", "op": "UPDATE", "row": {"ord_id": 17}}
    event = listener._build_event(data)
    assert event.item_id == 0


def test_on_notify_relays_to_event_bridge(event_bridge_spy) -> None:
    from services.core.db_event_listener import DbEventListener

    listener = DbEventListener(event_bridge=event_bridge_spy)
    payload = json.dumps({
        "event": "equip_task_txn_update",
        "table": "equip_task_txn",
        "op": "UPDATE",
        "row": {"txn_id": 50, "item_id": 29, "txn_stat": "SUCC"},
    })
    listener._on_notify(conn=None, pid=1, channel="lifecycle_event", payload=payload)
    assert len(event_bridge_spy.published) == 1
    evt = event_bridge_spy.published[0]
    assert evt.item_id == 29
    assert evt.payload["table"] == "equip_task_txn"
    assert evt.payload["row"]["txn_stat"] == "SUCC"


def test_on_notify_invalid_json_no_crash(event_bridge_spy) -> None:
    from services.core.db_event_listener import DbEventListener

    listener = DbEventListener(event_bridge=event_bridge_spy)
    listener._on_notify(conn=None, pid=1, channel="lifecycle_event", payload="not json")
    assert event_bridge_spy.published == []


def test_on_notify_non_dict_payload_no_crash(event_bridge_spy) -> None:
    from services.core.db_event_listener import DbEventListener

    listener = DbEventListener(event_bridge=event_bridge_spy)
    listener._on_notify(conn=None, pid=1, channel="lifecycle_event", payload='["not", "dict"]')
    assert event_bridge_spy.published == []


def test_normalize_dsn_strips_sqlalchemy_driver_prefix() -> None:
    from services.core.db_event_listener import _normalize_dsn

    assert _normalize_dsn("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
    assert _normalize_dsn("postgres+asyncpg://h/db") == "postgres://h/db"
    # 이미 정합 dsn 은 그대로 통과
    assert _normalize_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
