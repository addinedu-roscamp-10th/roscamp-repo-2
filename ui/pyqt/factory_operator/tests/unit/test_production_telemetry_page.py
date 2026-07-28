"""생산 telemetry source_available 정책과 throttle 검증."""

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

from app import mock_data
from app.generated import management_pb2
from app.management_client import ManagementClient
from app.pages.production import ProductionPage


class _TelemetryStubFake:
    def GetProductionTelemetrySnapshot(self, request, timeout):
        assert request.hours == 12
        assert timeout == 3.0
        return management_pb2.ProductionTelemetrySnapshotResponse(
            live=management_pb2.LiveParameterSection(
                source_available=True,
                value=management_pb2.LiveParameterEntry(
                    mold_pressure=11.0,
                    pour_angle=22.0,
                    furnace_heating_power=33.0,
                    cooling_progress=44.0,
                    cooling_current_temp=55.0,
                    cooling_target_temp=25.0,
                    cooling_remaining_min=6.0,
                ),
            ),
            temperature=management_pb2.TemperatureHistorySection(
                source_available=True,
                entries=[
                    management_pb2.TemperatureHistoryEntry(
                        minute=1.0,
                        temperature=100.0,
                        target=120.0,
                    )
                ],
            ),
            hourly=management_pb2.HourlyQualityProductionSection(
                source_available=False,
            ),
        )


class _RpcFailure(grpc.RpcError):
    pass


class _TelemetryClientFake:
    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self.calls = 0
        self.fail = False

    def get_production_telemetry_snapshot(self, *, hours: int):
        assert hours == 24
        self.calls += 1
        if self.fail:
            raise _RpcFailure()
        return deepcopy(self.snapshot)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _snapshot(*, source_available: bool) -> dict:
    return {
        "live": {
            "source_available": source_available,
            "value": {
                "mold_pressure": 11.0,
                "pour_angle": 22.0,
                "furnace_heating_power": 33.0,
                "cooling_progress": 44.0,
                "cooling_current_temp": 55.0,
                "cooling_target_temp": 25.0,
                "cooling_remaining_min": 6.0,
            },
        },
        "temperature": {
            "source_available": source_available,
            "entries": [
                {
                    "minute": 1.0,
                    "temperature": 100.0,
                    "target": 120.0,
                }
            ]
            if source_available
            else [],
        },
        "hourly": {
            "source_available": source_available,
            "entries": [
                {
                    "hour": "10:00",
                    "good": 7,
                    "bad": 1,
                }
            ]
            if source_available
            else [],
        },
    }


def test_management_client_preserves_telemetry_source_flags() -> None:
    client = object.__new__(ManagementClient)
    client._stub = _TelemetryStubFake()
    client._timeout = 3.0

    snapshot = client.get_production_telemetry_snapshot(hours=12)

    assert snapshot["live"]["source_available"] is True
    assert snapshot["live"]["value"]["mold_pressure"] == 11.0
    assert snapshot["temperature"]["entries"][0]["target"] == 120.0
    assert snapshot["hourly"] == {
        "source_available": False,
        "entries": [],
    }


def test_missing_sources_use_only_local_mock_and_show_labels(
    qt_app: QApplication,
) -> None:
    client = _TelemetryClientFake(_snapshot(source_available=False))
    page = ProductionPage(client)

    assert client.calls == 1
    assert page._gauge_pressure._value == mock_data.LIVE_PARAMETERS[
        "mold_pressure"
    ]
    assert page._temp_chart._actual.count() == len(
        mock_data.TEMPERATURE_HISTORY
    )
    assert page._hourly_chart._good_set.count() == len(
        mock_data.HOURLY_PRODUCTION
    )
    assert "실데이터 미연동" in page._gauge_title.text()
    assert "실데이터 미연동" in page._temp_chart.chart().title()
    assert "실데이터 미연동" in page._hourly_chart.chart().title()

    page.refresh()
    assert client.calls == 1

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_available_sources_use_real_values_without_mock(
    qt_app: QApplication,
) -> None:
    page = ProductionPage(
        _TelemetryClientFake(_snapshot(source_available=True))
    )

    assert page._gauge_pressure._value == 11.0
    assert page._temp_chart._actual.count() == 1
    assert page._temp_chart._actual.at(0).y() == 100.0
    assert page._hourly_chart._good_set.count() == 1
    assert page._hourly_chart._good_set.at(0) == 7.0
    assert "실데이터 미연동" not in page._gauge_title.text()
    assert "실데이터 미연동" not in page._temp_chart.chart().title()

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_available_empty_sections_do_not_use_mock(
    qt_app: QApplication,
) -> None:
    snapshot = _snapshot(source_available=True)
    snapshot["temperature"]["entries"] = []
    snapshot["hourly"]["entries"] = []
    page = ProductionPage(_TelemetryClientFake(snapshot))

    assert page._temp_chart._actual.count() == 0
    assert page._hourly_chart._good_set.count() == 0

    page.close()
    page.deleteLater()
    qt_app.processEvents()


def test_grpc_failure_preserves_last_telemetry(
    qt_app: QApplication,
) -> None:
    client = _TelemetryClientFake(_snapshot(source_available=True))
    page = ProductionPage(client)
    previous_pressure = page._gauge_pressure._value
    previous_temperature_count = page._temp_chart._actual.count()

    client.fail = True
    page._last_gauge_at = 0.0
    page._last_hourly_at = 0.0
    page.refresh()

    assert page._gauge_pressure._value == previous_pressure
    assert page._temp_chart._actual.count() == previous_temperature_count

    page.close()
    page.deleteLater()
    qt_app.processEvents()
