"""TaskTxnUpdateDispatcher (Phase 4a) 단위 테스트.

task_txn UPDATE(SUCC/FAIL) → in-process TASK_COMPLETED publish 검증.
SPEC: docs/db_event_bridge/SPEC.md §6 Phase 4 + PHASE4_ANALYSIS.md
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_event(table: str, op: str, row: dict, old_row: dict | None = None):
    from services.contracts.enums import EventType
    from services.contracts.models import Event

    return Event(
        event_type=EventType.DB_ROW_CHANGED,
        item_id=int(row.get("item_id") or 0),
        payload={
            "event": f"{table}_{op.lower()}",
            "table": table,
            "op": op,
            "row": row,
            "old_row": old_row,
            "at": "2026-05-15T09:18:23.731Z",
        },
    )


@pytest.fixture()
def bridge():
    b = MagicMock()
    b.published: list = []
    b.publish.side_effect = lambda evt: b.published.append(evt)
    return b


def test_dispatcher_disabled_by_default(monkeypatch, bridge) -> None:
    monkeypatch.delenv("MGMT_DB_EVENT_TASK_DISPATCH", raising=False)
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    d.handle(_make_event(
        "equip_task_txn", "UPDATE",
        row={"txn_id": 1, "item_id": 29, "task_type": "MM", "txn_stat": "SUCC"},
        old_row={"txn_stat": "PROC"},
    ))
    assert bridge.published == []


def test_dispatcher_publishes_task_completed_on_succ(monkeypatch, bridge) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.contracts.enums import EventType, TaskType
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    d.handle(_make_event(
        "equip_task_txn", "UPDATE",
        row={"txn_id": 1, "item_id": 29, "task_type": "MM", "txn_stat": "SUCC"},
        old_row={"txn_stat": "PROC"},
    ))
    assert len(bridge.published) == 1
    evt = bridge.published[0]
    assert evt.event_type == EventType.TASK_COMPLETED
    assert evt.item_id == 29
    assert evt.payload["task_type"] == TaskType.MM
    assert evt.payload["status"] == "SUCC"
    assert evt.payload["_origin"] == "db_event_router"


def test_dispatcher_publishes_on_fail(monkeypatch, bridge) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    d.handle(_make_event(
        "equip_task_txn", "UPDATE",
        row={"txn_id": 1, "item_id": 29, "task_type": "POUR", "txn_stat": "FAIL"},
        old_row={"txn_stat": "PROC"},
    ))
    assert len(bridge.published) == 1
    assert bridge.published[0].payload["status"] == "FAIL"


def test_dispatcher_skips_non_terminal_status(monkeypatch, bridge) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    # PROC 진입은 종결 신호 아님
    d.handle(_make_event(
        "equip_task_txn", "UPDATE",
        row={"txn_id": 1, "item_id": 29, "task_type": "MM", "txn_stat": "PROC"},
        old_row={"txn_stat": "QUE"},
    ))
    assert bridge.published == []


def test_dispatcher_skips_non_update_op(monkeypatch, bridge) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    # INSERT 는 Phase 4a 범위 밖
    d.handle(_make_event(
        "equip_task_txn", "INSERT",
        row={"txn_id": 1, "item_id": 29, "task_type": "MM", "txn_stat": "PROC"},
    ))
    assert bridge.published == []


def test_dispatcher_skips_same_status_update(monkeypatch, bridge) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    # 의미 있는 전이 아님 (SUCC → SUCC)
    d.handle(_make_event(
        "equip_task_txn", "UPDATE",
        row={"txn_id": 1, "item_id": 29, "task_type": "MM", "txn_stat": "SUCC"},
        old_row={"txn_stat": "SUCC"},
    ))
    assert bridge.published == []


def test_dispatcher_skips_untracked_table(monkeypatch, bridge) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    d.handle(_make_event(
        "ai_inference_txn", "UPDATE",
        row={"inference_id": 1, "item_id": 29, "txn_stat": "SUCC"},
        old_row={"txn_stat": "PROC"},
    ))
    assert bridge.published == []


def test_dispatcher_uses_fixed_task_type_for_insp(monkeypatch, bridge) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.contracts.enums import TaskType
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    # insp_task_txn 은 task_type 컬럼 없음 — 고정 TaskType.INSP 사용
    d.handle(_make_event(
        "insp_task_txn", "UPDATE",
        row={"txn_id": 14, "item_id": 29, "txn_stat": "SUCC"},
        old_row={"txn_stat": "PROC"},
    ))
    assert len(bridge.published) == 1
    assert bridge.published[0].payload["task_type"] == TaskType.INSP


def test_dispatcher_uses_fixed_task_type_for_pp(monkeypatch, bridge) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.contracts.enums import TaskType
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    d.handle(_make_event(
        "pp_task_txn", "UPDATE",
        row={"txn_id": 16, "item_id": 29, "pp_nm": "표면 연마", "txn_stat": "SUCC"},
        old_row={"txn_stat": "PROC"},
    ))
    assert len(bridge.published) == 1
    assert bridge.published[0].payload["task_type"] == TaskType.PP


def test_dispatcher_handles_trans_task_with_row_task_type(monkeypatch, bridge) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.contracts.enums import TaskType
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    d.handle(_make_event(
        "trans_task_txn", "UPDATE",
        row={"trans_task_txn_id": 100, "item_id": 29, "task_type": "ToPP", "txn_stat": "SUCC"},
        old_row={"txn_stat": "PROC"},
    ))
    assert len(bridge.published) == 1
    assert bridge.published[0].payload["task_type"] == TaskType.ToPP


def test_dispatcher_skips_unknown_task_type(monkeypatch, bridge) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import TaskTxnUpdateDispatcher

    d = TaskTxnUpdateDispatcher(event_bridge=bridge)
    d.handle(_make_event(
        "equip_task_txn", "UPDATE",
        row={"txn_id": 1, "item_id": 29, "task_type": "UNKNOWN_TYPE", "txn_stat": "SUCC"},
        old_row={"txn_stat": "PROC"},
    ))
    assert bridge.published == []
