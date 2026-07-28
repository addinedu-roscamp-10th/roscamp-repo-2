"""작업자 조회, 검사 완료, 핸드오프 명령 경로 검증."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import grpc
import management_pb2
import pytest

from rpc.field_event_rpc import FieldEventRpcMixin
from rpc.quality_rpc import QualityRpcMixin
from rpc.user_rpc import UserRpcMixin
from smart_cast_db.models import InspStat, InspTaskTxn, Item
from services.command.handoff_command_service import HandoffCommandService
from services.command.manual_inspection_command_service import (
    ManualInspectionCommandService,
    complete_inspection,
)
from services.contracts.enums import EventType


class _Session:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self) -> None:
        self.trace.append("commit")

    def query(self, model):
        return self

    def filter(self, *args):
        return self

    def first(self):
        return None


class _Bridge:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.events = []

    def publish(self, event):
        self.trace.append("event")
        self.events.append(event)


class _InspectionDb:
    def __init__(self) -> None:
        self.txn = SimpleNamespace(
            txn_id=17,
            item_id=29,
            txn_stat="PROC",
            result=None,
            req_at=datetime(2026, 7, 23, 11, 0, 0),
            start_at=datetime(2026, 7, 23, 11, 30, 0),
            end_at=None,
        )
        self.item = SimpleNamespace(
            result=None,
            updated_at=None,
        )
        self.stat = None
        self.flushed = False

    def get(self, model, key):
        if model is InspTaskTxn:
            return self.txn
        if model is InspStat:
            return self.stat
        if model is Item:
            return self.item
        raise AssertionError(model)

    def add(self, row) -> None:
        self.stat = row

    def flush(self) -> None:
        self.flushed = True


def test_management_inspection_command_updates_all_canonical_rows() -> None:
    db = _InspectionDb()
    completed_at = datetime(2026, 7, 23, 12, 0, 0)

    completed = complete_inspection(
        db,
        txn_id=17,
        result=False,
        completed_at=completed_at,
    )

    assert db.txn.txn_stat == "SUCC"
    assert db.txn.result is False
    assert db.txn.end_at == completed_at
    assert db.stat.item_id == 29
    assert db.stat.final_result == "DP"
    assert db.item.result is False
    assert db.item.updated_at == completed_at
    assert db.flushed is True
    assert completed.result == "NG"


def test_management_inspection_command_rejects_missing_transaction() -> None:
    db = _InspectionDb()
    db.txn = None

    with pytest.raises(LookupError, match="insp_task_txn=999 not found"):
        complete_inspection(db, txn_id=999, result=True)

    assert db.item.result is None
    assert db.flushed is False


def test_manual_inspection_commits_before_completion_event(monkeypatch) -> None:
    trace: list[str] = []
    completed = SimpleNamespace(
        txn_id=17,
        item_id=29,
        txn_stat="SUCC",
        result="OK",
        req_at=None,
        start_at=None,
        end_at=datetime(2026, 7, 23, 12, 0, 0),
    )

    def _complete(db, *, txn_id, result):
        assert txn_id == 17
        assert result is True
        trace.append("db_change")
        return completed

    monkeypatch.setattr(
        "services.command.manual_inspection_command_service.complete_inspection",
        _complete,
    )
    bridge = _Bridge(trace)
    service = ManualInspectionCommandService(
        event_bridge=bridge,
        session_factory=lambda: _Session(trace),
    )

    assert service.complete(txn_id=17, result=True) is completed
    assert trace == ["db_change", "commit", "event"]
    assert bridge.events[0].event_type is EventType.INSP_COMPLETED
    assert bridge.events[0].item_id == 29


def test_handoff_commits_before_waiter_release_event() -> None:
    trace: list[str] = []

    def _apply(db, **kwargs):
        assert kwargs["operator_id"] == 7
        assert kwargs["zone"] == "postprocessing"
        trace.append("db_change")
        return SimpleNamespace(
            released=True,
            task_id=31,
            amr_id="TAT01",
            item_id=41,
            ord_id=51,
            reason="done",
        )

    bridge = _Bridge(trace)
    service = HandoffCommandService(
        event_bridge=bridge,
        session_factory=lambda: _Session(trace),
        apply_func=_apply,
    )

    result = service.report(
        source_device="pyqt",
        zone="postprocessing",
        idempotency_key="idem-1",
        operator_id=7,
    )

    assert trace == ["db_change", "commit", "event"]
    assert result.released is True
    assert result.item_id == 41
    assert bridge.events[0].event_type is EventType.SUBTASK_COMPLETED
    assert bridge.events[0].payload["subtask_type"] == EventType.HANDOFF_ACK.value


class _DuplicateSession(_Session):
    def __init__(self, trace: list[str], duplicate) -> None:
        super().__init__(trace)
        self.duplicate = duplicate

    def first(self):
        return self.duplicate


def test_duplicate_handoff_does_not_commit_or_republish() -> None:
    trace: list[str] = []
    duplicate = SimpleNamespace(
        extra={
            "trans_task_txn_id": 31,
            "item_id": 41,
            "ord_id": 51,
        },
        amr_id="TAT01",
        ack_at=datetime(2026, 7, 23, 12, 0, 0),
        orphan_ack=False,
    )
    bridge = _Bridge(trace)
    service = HandoffCommandService(
        event_bridge=bridge,
        session_factory=lambda: _DuplicateSession(trace, duplicate),
    )

    result = service.report(
        source_device="pyqt",
        zone="postprocessing",
        idempotency_key="idem-1",
        operator_id=7,
    )

    assert trace == []
    assert result.reason == "duplicate"
    assert result.released is True
    assert result.item_id == 41


def test_orphan_handoff_commits_without_waiter_release_event() -> None:
    trace: list[str] = []

    def _apply(db, **kwargs):
        trace.append("db_change")
        return SimpleNamespace(
            released=False,
            task_id=None,
            amr_id=None,
            item_id=None,
            ord_id=None,
            reason="orphan_no_waiting_task",
        )

    service = HandoffCommandService(
        event_bridge=_Bridge(trace),
        session_factory=lambda: _Session(trace),
        apply_func=_apply,
    )

    result = service.report(
        source_device="pyqt",
        zone="postprocessing",
        idempotency_key=None,
        operator_id=None,
    )

    assert trace == ["db_change", "commit"]
    assert result.released is False
    assert result.reason == "orphan_no_waiting_task"


class _AbortError(Exception):
    def __init__(self, code, details) -> None:
        self.code = code
        self.details = details


class _Context:
    def abort(self, code, details):
        raise _AbortError(code, details)


class _Query:
    def __init__(self, row) -> None:
        self.row = row

    def filter(self, *args):
        return self

    def first(self):
        return self.row


class _UserSession:
    def __init__(self, row) -> None:
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def query(self, model):
        return _Query(self.row)


def test_get_operator_by_email_returns_not_found(monkeypatch) -> None:
    monkeypatch.setattr("rpc.user_rpc.SessionLocal", lambda: _UserSession(None))

    with pytest.raises(_AbortError) as caught:
        UserRpcMixin().GetOperatorByEmail(
            management_pb2.GetOperatorByEmailRequest(email="missing@example.com"),
            _Context(),
        )

    assert caught.value.code is grpc.StatusCode.NOT_FOUND


def test_get_operator_by_email_returns_operator(monkeypatch) -> None:
    row = SimpleNamespace(
        user_id=7,
        user_nm="작업자",
        email="worker@example.com",
        role="operator",
    )
    monkeypatch.setattr("rpc.user_rpc.SessionLocal", lambda: _UserSession(row))

    response = UserRpcMixin().GetOperatorByEmail(
        management_pb2.GetOperatorByEmailRequest(email="worker@example.com"),
        _Context(),
    )

    assert response.user_id == 7
    assert response.email == "worker@example.com"


def test_complete_inspection_rpc_maps_service_result() -> None:
    completed = SimpleNamespace(
        txn_id=17,
        item_id=29,
        txn_stat="SUCC",
        result="NG",
        req_at=None,
        start_at=None,
        end_at=datetime(2026, 7, 23, 12, 0, 0),
    )
    rpc = QualityRpcMixin()
    rpc.manual_inspection_command_service = SimpleNamespace(
        complete=lambda **kwargs: completed
    )

    response = rpc.CompleteInspection(
        management_pb2.CompleteInspectionRequest(txn_id=17, result=False),
        _Context(),
    )

    assert response.txn_id == 17
    assert response.item_id == 29
    assert response.result == "NG"
    assert response.end_at == "2026-07-23T12:00:00"


def test_handoff_rpc_maps_extended_response() -> None:
    rpc = FieldEventRpcMixin()
    rpc.handoff_command_service = SimpleNamespace(
        report=lambda **kwargs: SimpleNamespace(
            accepted=True,
            task_id="31",
            amr_id="TAT01",
            reason="released",
            ack_at=datetime(2026, 7, 23, 12, 0, 0),
            released=True,
            item_id=41,
            ord_id=51,
        )
    )

    response = rpc.ReportHandoffAck(
        management_pb2.HandoffAckEvent(
            source_device="pyqt",
            zone="postprocessing",
            operator_id=7,
        ),
        _Context(),
    )

    assert response.released is True
    assert response.item_id == 41
    assert response.ord_id == 51
