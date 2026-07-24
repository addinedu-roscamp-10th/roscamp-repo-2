"""MainWindow가 HTTP client 없이 gRPC client만 주입하는지 검증."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "factory_operator"))

import pytest
from PyQt5.QtWidgets import QApplication, QWidget

from app import main_window


class _Management:
    def close(self) -> None:
        return None


class _Page(QWidget):
    def __init__(self, *args) -> None:
        super().__init__()
        self.args = args


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_main_window_injects_only_management_client(
    qt_app: QApplication,
    monkeypatch,
) -> None:
    management = _Management()
    monkeypatch.setattr(main_window, "ManagementClient", lambda: management)
    for name in (
        "DashboardPage",
        "LogisticsPage",
        "OperationsPage",
        "PatternControlPage",
        "PpWorkerPage",
        "ProductionPage",
        "QualityPage",
        "StoragePage",
    ):
        monkeypatch.setattr(main_window, name, _Page)
    monkeypatch.setattr(main_window.MainWindow, "_start_refresh_timer", lambda self: None)
    monkeypatch.setattr(main_window.MainWindow, "_start_alert_stream", lambda self: None)
    monkeypatch.setattr(main_window.MainWindow, "_start_amr_status", lambda self: None)

    window = main_window.MainWindow()

    assert not hasattr(window, "_api")
    assert window._dashboard.args == (management,)
    assert window._pattern_control.args == ()
    assert window._operations.args == (management,)
    assert window._pp_worker.args == (management,)
    assert window._quality.args == (management,)

    window.close()
    window.deleteLater()
    qt_app.processEvents()
