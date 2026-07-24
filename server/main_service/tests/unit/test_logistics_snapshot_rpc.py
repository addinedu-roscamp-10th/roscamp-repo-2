"""물류와 창고 snapshot RPC 변환 검증."""

from __future__ import annotations

from rpc.logistics_rpc import LogisticsRpcMixin


class _LogisticsServiceFake:
    def get_snapshot(self):
        return {
            "tasks": [
                {
                    "txn_id": 1,
                    "res_id": "TAT1",
                    "task_type": "ToSTRG",
                    "txn_stat": "PROC",
                    "item_id": 10,
                    "ord_id": 20,
                    "req_at": "2026-07-23T10:00:00",
                    "start_at": "2026-07-23T10:01:00",
                    "end_at": "",
                }
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

    def list_warehouse_locations(self):
        return [
            {
                "loc_id": "7",
                "row": 2,
                "col": 1,
                "status": "occupied",
                "item_id": 10,
                "stored_at": "2026-07-23T11:10:00",
            }
        ]


class _LogisticsRpc(LogisticsRpcMixin):
    logistics_query_service = _LogisticsServiceFake()


def test_logistics_snapshot_contains_only_actual_task_and_order_fields() -> None:
    response = _LogisticsRpc().GetLogisticsSnapshot(
        request=object(),
        context=None,
    )

    assert response.tasks[0].txn_id == 1
    assert response.tasks[0].res_id == "TAT1"
    assert response.tasks[0].txn_stat == "PROC"
    assert response.tasks[0].item_id == 10
    assert response.orders[0].stat == "SHIPPING"


def test_warehouse_locations_preserve_actual_position_and_status() -> None:
    response = _LogisticsRpc().ListWarehouseLocations(
        request=object(),
        context=None,
    )

    assert response.locations[0].loc_id == "7"
    assert response.locations[0].row == 2
    assert response.locations[0].col == 1
    assert response.locations[0].status == "occupied"
    assert response.locations[0].item_id == 10
