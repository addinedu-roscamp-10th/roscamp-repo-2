"""물류와 창고 snapshot RPC."""

from __future__ import annotations

import management_pb2  # type: ignore


class LogisticsRpcMixin:
    """물류 작업, 출고 주문, 창고 위치를 실제 DB 값으로 제공."""

    def GetLogisticsSnapshot(self, request, context):
        snapshot = self.logistics_query_service.get_snapshot()
        return management_pb2.LogisticsSnapshotResponse(
            tasks=[
                management_pb2.TransportTaskEntry(
                    txn_id=row["txn_id"],
                    res_id=row["res_id"],
                    task_type=row["task_type"],
                    txn_stat=row["txn_stat"],
                    item_id=row["item_id"],
                    ord_id=row["ord_id"],
                    req_at=_iso(row["req_at"]),
                    start_at=_iso(row["start_at"]),
                    end_at=_iso(row["end_at"]),
                )
                for row in snapshot["tasks"]
            ],
            orders=[
                management_pb2.OutboundOrderEntry(
                    ord_id=row["ord_id"],
                    user_id=row["user_id"],
                    stat=row["stat"],
                    updated_at=_iso(row["updated_at"]),
                )
                for row in snapshot["orders"]
            ],
        )

    def ListWarehouseLocations(self, request, context):
        rows = self.logistics_query_service.list_warehouse_locations()
        return management_pb2.ListWarehouseLocationsResponse(
            locations=[
                management_pb2.WarehouseLocationEntry(
                    loc_id=row["loc_id"],
                    row=row["row"],
                    col=row["col"],
                    status=row["status"],
                    item_id=row["item_id"],
                    stored_at=_iso(row["stored_at"]),
                )
                for row in rows
            ]
        )


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.isoformat()
