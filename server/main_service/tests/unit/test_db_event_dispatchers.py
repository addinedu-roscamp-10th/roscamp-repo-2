"""InspTaskTxnDispatcher (Phase 3b) 단위 테스트.

AIAdapter / state_manager 를 mock 하여 dispatcher 의 분기/skip/dispatch 동작을 검증.
SPEC: docs/db_event_bridge/SPEC.md §6 Phase 3b
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_event(op: str, row: dict, item_id: int = 29):
    from services.contracts.enums import EventType
    from services.contracts.models import Event

    return Event(
        event_type=EventType.DB_ROW_CHANGED,
        item_id=item_id,
        payload={
            "event": f"insp_task_txn_{op.lower()}",
            "table": "insp_task_txn",
            "op": op,
            "row": row,
            "old_row": None,
            "at": "2026-05-15T09:18:23.731Z",
        },
    )


@pytest.fixture()
def mock_adapter():
    from services.contracts.models import AdapterResult

    adapter = MagicMock()
    adapter.execute.return_value = AdapterResult(
        success=True,
        message="ai_inference_completed",
        payload={
            "image_path": "/tmp/test.jpg",
            "inference": {"ok": True, "is_defective": True, "predicted_class": "CMH"},
        },
    )
    return adapter


@pytest.fixture()
def mock_state_manager():
    sm = MagicMock()
    sm.consume_inspection_image.return_value = {"image_path": "/tmp/test.jpg"}
    sm.record_inspection_result = MagicMock()  # async mock 은 별도 처리
    return sm


def test_dispatcher_disabled_by_default(monkeypatch, mock_adapter, mock_state_manager) -> None:
    monkeypatch.delenv("MGMT_DB_EVENT_TASK_DISPATCH", raising=False)
    from services.core.db_event_dispatchers import InspTaskTxnDispatcher

    d = InspTaskTxnDispatcher(adapter=mock_adapter, state_manager=mock_state_manager)
    d.handle(_make_event("INSERT", {"txn_id": 100, "item_id": 29, "txn_stat": "PROC"}))
    mock_adapter.execute.assert_not_called()
    mock_state_manager.consume_inspection_image.assert_not_called()


def test_dispatcher_skips_non_insert_op(monkeypatch, mock_adapter, mock_state_manager) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import InspTaskTxnDispatcher

    d = InspTaskTxnDispatcher(adapter=mock_adapter, state_manager=mock_state_manager)
    # UPDATE 는 Phase 3b 범위 밖
    d.handle(_make_event("UPDATE", {"txn_id": 100, "item_id": 29, "txn_stat": "SUCC"}))
    mock_adapter.execute.assert_not_called()


def test_dispatcher_skips_non_proc_status(monkeypatch, mock_adapter, mock_state_manager) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import InspTaskTxnDispatcher

    d = InspTaskTxnDispatcher(adapter=mock_adapter, state_manager=mock_state_manager)
    d.handle(_make_event("INSERT", {"txn_id": 100, "item_id": 29, "txn_stat": "QUE"}))
    mock_adapter.execute.assert_not_called()


def test_dispatcher_skips_when_image_registry_empty(
    monkeypatch, mock_adapter, mock_state_manager
) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import InspTaskTxnDispatcher

    mock_state_manager.consume_inspection_image.return_value = None  # 비어있음 (이미 in-process 가 소비)
    d = InspTaskTxnDispatcher(adapter=mock_adapter, state_manager=mock_state_manager)
    d.handle(_make_event("INSERT", {"txn_id": 100, "item_id": 29, "txn_stat": "PROC"}))
    mock_state_manager.consume_inspection_image.assert_called_once_with(29)
    mock_adapter.execute.assert_not_called()  # registry 비어있으면 AIAdapter 안 부름


def test_dispatcher_skips_when_image_path_missing(
    monkeypatch, mock_adapter, mock_state_manager
) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import InspTaskTxnDispatcher

    mock_state_manager.consume_inspection_image.return_value = {"image_path": "/nonexistent.jpg"}
    d = InspTaskTxnDispatcher(adapter=mock_adapter, state_manager=mock_state_manager)
    d.handle(_make_event("INSERT", {"txn_id": 100, "item_id": 29, "txn_stat": "PROC"}))
    mock_adapter.execute.assert_not_called()  # 파일 없으면 호출 안 함


def test_dispatcher_dispatches_adapter_and_record(
    tmp_path: Path, monkeypatch, mock_adapter, mock_state_manager
) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import InspTaskTxnDispatcher

    # 실 파일 생성 (Path.exists() 통과)
    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0sim\xff\xd9")
    mock_state_manager.consume_inspection_image.return_value = {"image_path": str(img)}

    # async record_inspection_result coroutine 시뮬레이션
    record_called: dict[str, object] = {}

    async def fake_record(*, item_id: int, inference: dict):
        record_called["item_id"] = item_id
        record_called["inference_keys"] = sorted((inference or {}).keys())
        return True

    mock_state_manager.record_inspection_result = fake_record

    d = InspTaskTxnDispatcher(adapter=mock_adapter, state_manager=mock_state_manager)
    loop = asyncio.new_event_loop()

    # loop 를 background thread 로 실행 — run_coroutine_threadsafe 가 필요
    import threading

    def _run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()
    try:
        d.set_loop(loop)
        d.handle(_make_event("INSERT", {"txn_id": 100, "item_id": 29, "txn_stat": "PROC"}))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2.0)
        loop.close()

    mock_adapter.execute.assert_called_once()
    assert record_called["item_id"] == 29
    assert "ok" in record_called["inference_keys"]


def test_dispatcher_loop_not_set_warns_but_no_crash(
    tmp_path: Path, monkeypatch, mock_adapter, mock_state_manager
) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import InspTaskTxnDispatcher

    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0sim\xff\xd9")
    mock_state_manager.consume_inspection_image.return_value = {"image_path": str(img)}

    d = InspTaskTxnDispatcher(adapter=mock_adapter, state_manager=mock_state_manager)
    # set_loop() 호출 안 함 — record 스킵 되어야 함, AIAdapter 는 호출됨
    d.handle(_make_event("INSERT", {"txn_id": 100, "item_id": 29, "txn_stat": "PROC"}))
    mock_adapter.execute.assert_called_once()  # AIAdapter 는 sync 라 정상 호출


def test_dispatcher_adapter_exception_no_crash(
    tmp_path: Path, monkeypatch, mock_adapter, mock_state_manager
) -> None:
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    from services.core.db_event_dispatchers import InspTaskTxnDispatcher

    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0sim\xff\xd9")
    mock_state_manager.consume_inspection_image.return_value = {"image_path": str(img)}
    mock_adapter.execute.side_effect = RuntimeError("network down")

    d = InspTaskTxnDispatcher(adapter=mock_adapter, state_manager=mock_state_manager)
    # 예외 격리 — dispatcher 자체는 crash 안 함
    d.handle(_make_event("INSERT", {"txn_id": 100, "item_id": 29, "txn_stat": "PROC"}))
    mock_adapter.execute.assert_called_once()
