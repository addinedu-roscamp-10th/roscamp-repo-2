"""Operations snapshot client 변환과 worker thread 실행 검증."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import grpc

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "factory_operator"))

from PyQt5.QtCore import QThread
from PyQt5.QtTest import QSignalSpy
from PyQt5.QtWidgets import QApplication

from app.generated import management_pb2
from app.management_client import ManagementClient
from app.pages.operations import OperationsPage, _RefreshWorker


class _OperationsStubFake:
    def GetOperationsSnapshot(self, request, timeout):
        assert request.hours == 12
        assert timeout == 3.0
        return management_pb2.OperationsSnapshotResponse(
            summary=[
                management_pb2.InspectionSummaryEntry(
                    ord_id=10,
                    total_items=4,
                    good_count=2,
                    defective_count=1,
                    pending_count=1,
                )
            ],
            hourly=[
                management_pb2.HourlyProductionEntry(
                    hour="2026-07-23T10:00:00",
                    count=3,
                )
            ],
            err_trend=[
                management_pb2.DefectRateTrendEntry(
                    hour="2026-07-23T10:00:00",
                    equip=2,
                )
            ],
            dashboard={
                "total_orders": "5",
                "defect_rate_pct": "25.0",
                "timescaledb_enabled": "False",
                "snapshot_at": "2026-07-23T10:00:00",
            },
            orders=[
                management_pb2.ProductionOrder(
                    ord_id=10,
                    user_id=20,
                    latest_stat="MFG",
                    company_name="회사",
                    customer_name="담당자",
                    created_at="2026-07-23T09:00:00",
                    prod_id=3,
                )
            ],
            patterns=[
                management_pb2.PatternAssignment(
                    ord_id=10,
                    pattern_id=3,
                    ptn_loc_id=7,
                )
            ],
            stages=[
                management_pb2.StageEntry(
                    zone_id=1,
                    zone_nm="MOLD",
                    in_progress_count=2,
                )
            ],
            items=[
                management_pb2.Item(
                    id=30,
                    order_id="10",
                    cur_stage=management_pb2.ITEM_STAGE_MM,
                    curr_res="MM01",
                    mfg_at=management_pb2.Timestamp(
                        iso8601="2026-07-23T10:00:00"
                    ),
                    flow_stat="CAST",
                    zone_nm="MOLD",
                    cur_stat="MM",
                )
            ],
        )


class _WorkerClientFake:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.called_thread_id: int | None = None
        self.closed = False

    def get_operations_snapshot(self, *, hours: int):
        assert hours == 24
        self.snapshot_calls += 1
        self.called_thread_id = int(QThread.currentThreadId())
        return {
            "summary": [{"ord_id": 10}],
            "hourly": [{"bucket": "10", "produced": 3}],
            "err_trend": [],
            "dashboard": {"timescaledb_enabled": False},
            "orders": [
                {
                    "ord_id": 10,
                    "created_at": "2026-07-23T09:00:00",
                    "prod_id": 3,
                }
            ],
            "patterns": [{"ord_id": 10, "ptn_loc_id": 7}],
            "stages": [{"zone_id": 1, "zone_nm": "MOLD"}],
            "items": [
                {
                    "item_id": 30,
                    "ord_id": 10,
                    "cur_stat": "MM",
                }
            ],
        }

    def list_production_orders(self, **kwargs):
        raise AssertionError("정기 refresh에서 별도 주문 RPC를 호출하면 안 됨")

    def list_patterns(self):
        raise AssertionError("정기 refresh에서 별도 패턴 RPC를 호출하면 안 됨")

    def list_stages(self):
        raise AssertionError("정기 refresh에서 별도 공정 RPC를 호출하면 안 됨")

    def list_item_views(self, **kwargs):
        raise AssertionError("정기 refresh에서 별도 품목 RPC를 호출하면 안 됨")

    def close(self) -> None:
        self.closed = True


class _RpcFailure(grpc.RpcError):
    pass


class _WorkerFailureClientFake(_WorkerClientFake):
    def get_operations_snapshot(self, *, hours: int):
        raise _RpcFailure()


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_management_client_maps_operations_snapshot() -> None:
    client = object.__new__(ManagementClient)
    client._stub = _OperationsStubFake()
    client._timeout = 3.0

    snapshot = client.get_operations_snapshot(hours=12)

    assert snapshot["summary"][0]["pending_count"] == 1
    assert snapshot["hourly"][0]["produced"] == 3
    assert snapshot["err_trend"] == [
        {
            "bucket": "2026-07-23T10:00:00",
            "source": "equip",
            "count": 2,
        }
    ]
    assert snapshot["dashboard"]["total_orders"] == 5
    assert snapshot["dashboard"]["defect_rate_pct"] == 25.0
    assert snapshot["dashboard"]["timescaledb_enabled"] is False
    assert snapshot["orders"][0]["prod_id"] == 3
    assert snapshot["patterns"][0]["ptn_loc_id"] == 7
    assert snapshot["stages"][0]["status"] == "2건 진행중"
    assert snapshot["items"][0]["cur_stat"] == "MM"


def test_refresh_worker_calls_one_snapshot_off_gui_thread(
    qt_app: QApplication,
) -> None:
    client = _WorkerClientFake()
    worker = _RefreshWorker(client_factory=lambda: client)
    thread = QThread()
    worker.moveToThread(thread)
    rows: list[dict] = []
    main_thread_id = int(QThread.currentThreadId())

    worker.data_ready.connect(rows.append)
    worker.data_ready.connect(lambda _: thread.quit())
    thread.started.connect(worker.run)
    finished = QSignalSpy(thread.finished)
    thread.start()

    assert finished.wait(3000)
    assert client.snapshot_calls == 1
    assert client.called_thread_id != main_thread_id
    assert client.closed is True
    assert rows[0]["summary"] == [{"ord_id": 10}]
    assert rows[0]["orders"][0]["ord_id"] == 10
    assert rows[0]["patterns"][0]["ptn_loc_id"] == 7
    assert rows[0]["stages"][0]["zone_nm"] == "MOLD"
    assert rows[0]["item_progress"] == [
        {
            "order_id": "10",
            "product": "원형 맨홀뚜껑 KS D-550",
            "item": "ord_10_item_20260723_30",
            "stage": "주탕",
            "stage_code": "MM",
        }
    ]
    assert not hasattr(OperationsPage, "_fetch_items_via_grpc")

    worker.deleteLater()
    thread.deleteLater()
    qt_app.processEvents()


def test_snapshot_failure_marker_keeps_existing_page_state() -> None:
    client = _WorkerFailureClientFake()
    worker = _RefreshWorker(client_factory=lambda: client)
    rows: list[dict] = []
    worker.data_ready.connect(rows.append)

    worker.run()

    assert rows == [{"snapshot_failed": True}]
    assert client.closed is True
    OperationsPage._on_refresh_done(
        object(),
        rows[0],
    )
