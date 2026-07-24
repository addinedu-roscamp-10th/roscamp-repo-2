"""운영 현황 화면 snapshot RPC."""

from __future__ import annotations

import management_pb2  # type: ignore

from rpc.proto_helpers import (
    item_to_proto,
    pattern_to_proto,
    production_order_to_proto,
    stage_to_proto,
)


class OperationsRpcMixin:
    """운영 현황 조회를 unary snapshot 한 번으로 제공."""

    def GetOperationsSnapshot(self, request, context):
        snapshot = self.operations_query_service.get_snapshot(
            hours=request.hours or 24,
        )
        return management_pb2.OperationsSnapshotResponse(
            summary=[
                management_pb2.InspectionSummaryEntry(
                    ord_id=row["ord_id"],
                    total_items=row["total_items"],
                    good_count=row["good_count"],
                    defective_count=row["defective_count"],
                    pending_count=row["pending_count"],
                )
                for row in snapshot["summary"]
            ],
            hourly=[
                management_pb2.HourlyProductionEntry(
                    hour=row["bucket"],
                    count=row["produced"],
                )
                for row in snapshot["hourly"]
            ],
            err_trend=[
                management_pb2.DefectRateTrendEntry(
                    hour=row["bucket"],
                    equip=row["count"] if row["source"] == "equip" else 0,
                    trans=row["count"] if row["source"] == "trans" else 0,
                )
                for row in snapshot["err_trend"]
            ],
            dashboard={
                key: str(value)
                for key, value in snapshot["dashboard"].items()
            },
            orders=[
                production_order_to_proto(row)
                for row in snapshot["orders"]
            ],
            patterns=[
                pattern_to_proto(row)
                for row in snapshot["patterns"]
            ],
            stages=[
                stage_to_proto(row)
                for row in snapshot["stages"]
            ],
            items=[
                item_to_proto(row)
                for row in snapshot["items"]
            ],
        )
