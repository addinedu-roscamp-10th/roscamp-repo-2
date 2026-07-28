"""PyQt Management command wrapper 계약 검증."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "factory_operator"))

from app.generated import management_pb2
from app.management_client import ManagementClient


class _CommandStub:
    def GetOperatorByEmail(self, request, timeout):
        assert request.email == "worker@example.com"
        assert timeout == 3.0
        return management_pb2.OperatorRow(
            user_id=7,
            user_nm="작업자",
            email=request.email,
            role="operator",
        )

    def CompleteInspection(self, request, timeout):
        assert request.txn_id == 17
        assert request.result is False
        return management_pb2.InspectionEntry(
            txn_id=17,
            item_id=29,
            txn_stat="SUCC",
            result="NG",
            end_at="2026-07-23T12:00:00",
        )

    def ReportHandoffAck(self, request, timeout):
        assert request.source_device == "pyqt-test"
        assert request.HasField("operator_id")
        assert request.operator_id == 7
        return management_pb2.HandoffAckResponse(
            accepted=True,
            task_id="31",
            amr_id="TAT01",
            reason="released",
            ack_at="2026-07-23T12:00:00",
            released=True,
            item_id=41,
            ord_id=51,
        )


def _client() -> ManagementClient:
    client = object.__new__(ManagementClient)
    client._stub = _CommandStub()
    client._timeout = 3.0
    client._operator = None
    return client


def test_operator_login_state_is_shared_with_handoff() -> None:
    client = _client()

    operator = client.get_operator_by_email("worker@example.com")
    response = client.report_handoff_ack(source_device="pyqt-test")

    assert operator["user_id"] == 7
    assert client.current_operator_id() == 7
    assert client.current_operator_label() == "작업자 (operator) #7"
    assert response["released"] is True
    assert response["item_id"] == 41


def test_complete_inspection_maps_response() -> None:
    response = _client().complete_inspection(17, False)

    assert response["txn_stat"] == "SUCC"
    assert response["result"] == "NG"
    assert response["end_at"] == "2026-07-23T12:00:00"


def test_logout_clears_operator_state() -> None:
    client = _client()
    client.get_operator_by_email("worker@example.com")

    client.clear_operator()

    assert client.current_operator_id() is None
    assert client.current_operator_label() == "비로그인"
