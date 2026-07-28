"""생산 telemetry 실데이터 미연동 응답 검증."""

from __future__ import annotations

from rpc.production_telemetry_rpc import ProductionTelemetryRpcMixin


class _TelemetryRpc(ProductionTelemetryRpcMixin):
    pass


class _Request:
    hours = 24


def test_telemetry_rpc_marks_all_missing_sources_unavailable() -> None:
    response = _TelemetryRpc().GetProductionTelemetrySnapshot(
        _Request(),
        context=None,
    )

    assert response.live.source_available is False
    assert response.temperature.source_available is False
    assert response.hourly.source_available is False
    assert list(response.temperature.entries) == []
    assert list(response.hourly.entries) == []
