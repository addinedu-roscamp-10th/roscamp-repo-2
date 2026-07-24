"""물류와 창고 gRPC 실데이터 표시 정책 검증."""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from pathlib import Path

import grpc
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "factory_operator"))

from PyQt5.QtWidgets import QApplication

from app.generated import management_pb2
from app.management_client import ManagementClient
from app.pages.logistics import LogisticsPage
from app.pages.storage import StoragePage


class _LogisticsStubFake:
    def GetLogisticsSnapshot(self, request, timeout):
        assert timeout == 3.0
        return management_pb2.LogisticsSnapshotResponse(
            tasks=[
                management_pb2.TransportTaskEntry(
                    txn_id=1,
                    res_id="TAT1",
                    task_type="ToSTRG",
                    txn_stat="PROC",
                    item_id=10,
                    ord_id=20,
                    req_at="2026-07-23T10:00:00",
                )
            ],
            orders=[
                management_pb2.OutboundOrderEntry(
                    ord_id=20,
                    user_id=30,
                    stat="SHIPPING",
                    updated_at="2026-07-23T11:00:00",
                )
            ],
        )

    def ListWarehouseLocations(self, request, timeout):
        assert timeout == 3.0
        return management_pb2.ListWarehouseLocationsResponse(
            locations=[
                management_pb2.WarehouseLocationEntry(
                    loc_id="7",
                    row=2,
                    col=1,
                    status="occupied",
                    item_id=10,
                    stored_at="2026-07-23T11:10:00",
                )
            ]
        )


class _RpcFailure(grpc.RpcError):
    pass


class _LogisticsClientFake:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.location_calls = 0
        self.fail_snapshot = False
        self.fail_locations = False
        self.snapshot = {
            "tasks": [
                {
                    "txn_id": 1,
                    "res_id": "TAT1",
                    "task_type": "ToSTRG",
                    "txn_stat": "PROC",
                    "item_id": 10,
                    "ord_id": 20,
                    "req_at": "2026-07-23T10:00:00",
                    "start_at": "",
                    "end_at": "",
                },
                {
                    "txn_id": 2,
                    "res_id": "TAT2",
                    "task_type": "ToSHIP",
                    "txn_stat": "QUE",
                    "item_id": 11,
                    "ord_id": 21,
                    "req_at": "2026-07-23T10:01:00",
                    "start_at": "",
                    "end_at": "",
                },
                {
                    "txn_id": 3,
                    "res_id": "TAT3",
                    "task_type": "ToPP",
                    "txn_stat": "SUCC",
                    "item_id": 12,
                    "ord_id": 22,
                    "req_at": "2026-07-23T10:02:00",
                    "start_at": "",
                    "end_at": "2026-07-23T10:03:00",
                },
            ],
            "orders": [
                {
                    "ord_id": 20,
                    "user_id": 30,
                    "stat": "SHIPPING",
                    "updated_at": "2026-07-23T11:00:00",
                }
            ],
        }
        self.locations = [
            {
                "loc_id": "7",
                "row": 1,
                "col": 1,
                "status": "occupied",
                "item_id": 10,
                "stored_at": "2026-07-23T11:10:00",
            },
            {
                "loc_id": "8",
                "row": 1,
                "col": 2,
                "status": "reserved",
                "item_id": None,
                "stored_at": "",
            },
        ]

    def get_logistics_snapshot(self):
        self.snapshot_calls += 1
        if self.fail_snapshot:
            raise _RpcFailure()
        return deepcopy(self.snapshot)

    def list_warehouse_locations(self):
        self.location_calls += 1
        if self.fail_locations:
            raise _RpcFailure()
        return deepcopy(self.locations)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_management_client_maps_logistics_and_storage() -> None:
    client = object.__new__(ManagementClient)
    client._stub = _LogisticsStubFake()
    client._timeout = 3.0

    snapshot = client.get_logistics_snapshot()
    locations = client.list_warehouse_locations()

    assert snapshot["tasks"][0]["txn_stat"] == "PROC"
    assert snapshot["orders"][0]["stat"] == "SHIPPING"
    assert locations == [
        {
            "loc_id": "7",
            "row": 2,
            "col": 1,
            "status": "occupied",
            "item_id": 10,
            "stored_at": "2026-07-23T11:10:00",
        }
    ]


def test_logistics_page_uses_one_snapshot_and_actual_statuses(
    qt_app: QApplication,
) -> None:
    client = _LogisticsClientFake()
    page = LogisticsPage(client)
    page.update_amr_live(
        [
            {
                "id": "TAT1",
                "task_state": 1,
                "status": "running",
                "battery": 80,
            }
        ]
    )
    page.refresh()

    assert client.snapshot_calls == 2
    assert page._task_table.columnCount() == 7
    assert page._task_table.rowCount() == 3
    assert page._task_table.item(0, 5).text() == "진행 중"
    assert page._outbound_table.columnCount() == 4
    assert page._outbound_table.item(0, 2).text() == "출고 중"
    assert page._kpi_cards["active_tasks"]._value.text() == "1"
    assert page._kpi_cards["pending_tasks"]._value.text() == "1"
    assert page._kpi_cards["completed_tasks"]._value.text() == "1"
    assert page._kpi_cards["idle_amr"]._value.text() == "1"

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_real_empty_logistics_response_stays_empty(
    qt_app: QApplication,
) -> None:
    client = _LogisticsClientFake()
    client.snapshot = {"tasks": [], "orders": []}
    page = LogisticsPage(client)

    assert page._task_table.rowCount() == 0
    assert page._outbound_table.rowCount() == 0

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_storage_page_uses_locations_and_empty_response_resets_rack(
    qt_app: QApplication,
) -> None:
    client = _LogisticsClientFake()
    page = StoragePage(client)

    assert client.location_calls == 1
    assert page._detail_table.columnCount() == 6
    assert page._detail_table.item(0, 4).text() == "10"
    assert page._kpi_cards["occupied"]._value.text() == "1"
    assert page._kpi_cards["reserved"]._value.text() == "1"
    assert page._rack_widget._scene._cells["1"]._status == "occupied"

    client.locations = []
    page.refresh()

    assert page._detail_table.rowCount() == 0
    assert page._rack_widget._scene._cells["1"]._status == "empty"

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_grpc_failures_preserve_last_logistics_and_storage_state(
    qt_app: QApplication,
) -> None:
    client = _LogisticsClientFake()
    logistics = LogisticsPage(client)
    storage = StoragePage(client)
    task_rows = logistics._task_table.rowCount()
    location_rows = storage._detail_table.rowCount()

    client.fail_snapshot = True
    client.fail_locations = True
    logistics.refresh()
    storage.refresh()

    assert logistics._task_table.rowCount() == task_rows
    assert storage._detail_table.rowCount() == location_rows

    logistics.close()
    storage.close()
    logistics.deleteLater()
    storage.deleteLater()
    qt_app.processEvents()
