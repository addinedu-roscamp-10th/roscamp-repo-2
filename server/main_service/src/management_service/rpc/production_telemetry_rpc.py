"""생산 telemetry 실데이터 연동 상태 RPC."""

from __future__ import annotations

import management_pb2  # type: ignore


class ProductionTelemetryRpcMixin:
    """실데이터가 연결되기 전에는 미연동 section만 반환."""

    def GetProductionTelemetrySnapshot(self, request, context):
        return management_pb2.ProductionTelemetrySnapshotResponse(
            live=management_pb2.LiveParameterSection(
                source_available=False,
            ),
            temperature=management_pb2.TemperatureHistorySection(
                source_available=False,
            ),
            hourly=management_pb2.HourlyQualityProductionSection(
                source_available=False,
            ),
        )
