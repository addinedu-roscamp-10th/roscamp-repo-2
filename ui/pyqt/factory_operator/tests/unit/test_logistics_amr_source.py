"""물류 화면 AMR 데이터가 Management gRPC 수신값만 사용하는지 검증."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "factory_operator"))

from PyQt5.QtWidgets import QApplication

from app.pages.logistics import LogisticsPage


class _LogisticsClientFake:
    """AMR 조회 메서드가 없는 물류 snapshot fake."""

    def get_logistics_snapshot(self) -> dict:
        return {"tasks": [], "orders": []}


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_initial_refresh_does_not_use_http_or_mock_amr(qt_app: QApplication) -> None:
    page = LogisticsPage(_LogisticsClientFake())

    assert page._amr_live == []
    assert page._amr_cards == {}

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_refresh_keeps_last_grpc_amr_state(qt_app: QApplication) -> None:
    page = LogisticsPage(_LogisticsClientFake())
    received = [
        {
            "id": "TAT1",
            "type": "amr",
            "status": "running",
            "battery": 72.0,
            "location": "x=1.00, y=2.00",
            "task_state": 2,
        }
    ]

    page.update_amr_live(received)
    page.refresh()

    assert page._amr_live == received
    assert set(page._amr_cards) == {"TAT1"}

    page.close()
    page.deleteLater()
    qt_app.processEvents()
