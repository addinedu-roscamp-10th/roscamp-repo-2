"""품질 화면의 snapshot 공유와 source_available 정책 검증."""

from __future__ import annotations

import base64
import os
import sys
from copy import deepcopy
from pathlib import Path

import grpc
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "factory_operator"))

from PyQt5.QtWidgets import QApplication

from app import mock_data
from app.generated import management_pb2
from app.management_client import ManagementClient
from app.pages.quality import QualityPage


class _RpcFailure(grpc.RpcError):
    pass


class _QualityClientFake:
    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self.calls = 0
        self.image_calls: list[tuple[int, str]] = []
        self.fail = False

    def get_quality_snapshot(self, *, hours: int, inspection_limit: int) -> dict:
        assert hours == 24
        assert inspection_limit == 200
        self.calls += 1
        if self.fail:
            raise _RpcFailure()
        return deepcopy(self.snapshot)

    def get_inspection_image(
        self,
        inference_id: int,
        *,
        kind: str,
    ) -> dict:
        self.image_calls.append((inference_id, kind))
        if self.fail:
            raise _RpcFailure()
        return {
            "image_bytes": base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ),
            "content_type": "image/png",
        }


class _QualityStubFake:
    def GetQualitySnapshot(self, request, timeout):
        assert request.hours == 12
        assert request.inspection_limit == 50
        assert timeout == 3.0
        return management_pb2.QualitySnapshotResponse(
            stats=management_pb2.QualityStats(
                inspected=4,
                good=3,
                defective=1,
                pending=2,
                defect_rate=25.0,
            ),
            defect_types=management_pb2.DefectTypeSection(
                source_available=True,
            ),
            standards=management_pb2.InspectionStandardSection(
                source_available=False,
            ),
            production_vs_defects=management_pb2.ProductionVsDefectSection(
                source_available=True,
            ),
            inspections=[
                management_pb2.InspectionEntry(
                    txn_id=7,
                    item_id=8,
                    inference_id=9,
                )
            ],
        )

    def GetInspectionImage(self, request, timeout):
        assert request.inference_id == 9
        assert request.kind == management_pb2.INSPECTION_IMAGE_KIND_RESULT
        assert timeout == 3.0
        return management_pb2.GetInspectionImageResponse(
            image_bytes=b"grpc-image",
            content_type="image/png",
        )


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _snapshot(
    *,
    source_available: bool,
    inference_id: int | None = None,
) -> dict:
    return {
        "stats": {
            "total": 1,
            "ok": 0,
            "ng": 1,
            "pending": 0,
            "defect_rate": 100.0,
        },
        "inspections": [
            {
                "id": "10",
                "txn_id": 10,
                "item_id": 20,
                "inference_id": inference_id,
                "casting_id": "20",
                "txn_stat": "PROC",
                "result": "",
                "req_at": "2026-07-23T10:00:00",
                "start_at": "2026-07-23T10:01:00",
                "end_at": "",
                "inspected_at": "2026-07-23T10:01:00",
                "product": "원형 맨홀",
                "defect_type": "",
                "inspector": "",
                "note": "",
                "confidence": 0.0,
            }
        ],
        "trend": [{"label": "07/23 10시", "rate": 100.0}],
        "defect_types": {
            "source_available": source_available,
            "entries": [],
        },
        "standards": {
            "source_available": source_available,
            "entries": [],
        },
        "production_vs_defects": {
            "source_available": source_available,
            "entries": [],
        },
    }


def test_management_client_preserves_source_flags_and_empty_entries() -> None:
    client = object.__new__(ManagementClient)
    client._stub = _QualityStubFake()
    client._timeout = 3.0

    snapshot = client.get_quality_snapshot(hours=12, inspection_limit=50)

    assert snapshot["stats"]["defect_rate"] == 25.0
    assert snapshot["inspections"][0]["inference_id"] == 9
    assert snapshot["defect_types"] == {
        "source_available": True,
        "entries": [],
    }
    assert snapshot["standards"] == {
        "source_available": False,
        "entries": [],
    }

    image = client.get_inspection_image(9, kind="result")
    assert image == {
        "image_bytes": b"grpc-image",
        "content_type": "image/png",
    }


def test_quality_page_uses_one_snapshot_and_explicit_mock_sections(
    qt_app: QApplication,
) -> None:
    client = _QualityClientFake(_snapshot(source_available=False))
    page = QualityPage(client)

    assert client.calls == 1
    assert page._table.rowCount() == 1
    assert page._proc_table.rowCount() == 1
    assert page._standards._items == mock_data.INSPECTION_STANDARDS
    assert "실데이터 미연동" in page._top_defects._title.text()
    assert "실데이터 미연동" in page._pie_chart.chart().title()
    assert "실데이터 미연동" in page._vs_chart.chart().title()

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_source_available_true_with_empty_entries_does_not_use_mock(
    qt_app: QApplication,
) -> None:
    page = QualityPage(_QualityClientFake(_snapshot(source_available=True)))

    assert page._standards._items == []
    assert page._top_defects._badges[0][1].text() == "-"
    assert page._pie_chart._series.count() == 0
    assert "실데이터 미연동" not in page._top_defects._title.text()
    assert "실데이터 미연동" not in page._vs_chart.chart().title()

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_grpc_failure_keeps_last_normal_snapshot(qt_app: QApplication) -> None:
    client = _QualityClientFake(_snapshot(source_available=True))
    page = QualityPage(client)
    previous_rows = list(page._inspections_cache)

    client.fail = True
    page.refresh()

    assert page._inspections_cache == previous_rows
    assert page._table.rowCount() == 1

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_quality_page_loads_each_inference_image_once_via_grpc(
    qt_app: QApplication,
) -> None:
    client = _QualityClientFake(
        _snapshot(source_available=True, inference_id=31)
    )
    page = QualityPage(client)

    assert client.image_calls == [(31, "result")]
    assert page._vision_feed._last_pixmap is not None
    assert not page._vision_feed._last_pixmap.isNull()
    assert not page._table.item(0, 0).icon().isNull()
    assert page._table.rowHeight(0) >= 62

    page.refresh()
    assert client.image_calls == [(31, "result")]

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_failed_image_switch_clears_loaded_inference_cache(
    qt_app: QApplication,
) -> None:
    client = _QualityClientFake(
        _snapshot(source_available=True, inference_id=31)
    )
    page = QualityPage(client)
    assert page._loaded_inference_id == 31

    client.fail = True
    page._load_inspection_image({"inference_id": 32})
    assert page._loaded_inference_id is None
    assert page._vision_feed._last_pixmap is None

    client.fail = False
    page._load_inspection_image({"inference_id": 31})
    assert client.image_calls == [
        (31, "result"),
        (32, "result"),
        (31, "result"),
    ]
    assert page._loaded_inference_id == 31

    page.close()
    page.deleteLater()
    qt_app.processEvents()
