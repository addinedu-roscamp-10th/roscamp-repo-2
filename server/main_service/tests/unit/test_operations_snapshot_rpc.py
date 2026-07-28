"""Operations snapshot 조회와 RPC 응답 변환 검증."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from rpc.operations_rpc import OperationsRpcMixin
from services.query import operations_query_service
from services.query.operations_query_service import OperationsQueryService


class _SessionContextFake:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _ItemQueryServiceFake:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def list_stages(self):
        self.calls.append(("list_stages",))
        return ["stage"]

    def list_items(self, *, order_id, stage, limit):
        self.calls.append(("list_items", order_id, stage, limit))
        return ["item"]


class _PatternQueryServiceFake:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def list_patterns(self):
        self.calls.append(("list_patterns",))
        return ["pattern"]


class _ProductionOrderQueryServiceFake:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def list_orders(self, *, status_filters, limit):
        self.calls.append(("list_orders", status_filters, limit))
        return ["order"]


class _OperationsServiceFake:
    def get_snapshot(self, *, hours: int):
        assert hours == 24
        return {
            "summary": [
                {
                    "ord_id": 10,
                    "total_items": 4,
                    "good_count": 2,
                    "defective_count": 1,
                    "pending_count": 1,
                }
            ],
            "hourly": [
                {
                    "bucket": "2026-07-23T10:00:00",
                    "produced": 3,
                }
            ],
            "err_trend": [
                {
                    "bucket": "2026-07-23T10:00:00",
                    "source": "equip",
                    "count": 2,
                },
                {
                    "bucket": "2026-07-23T11:00:00",
                    "source": "trans",
                    "count": 1,
                },
            ],
            "dashboard": {
                "total_orders": 5,
                "timescaledb_enabled": False,
            },
            "orders": [
                SimpleNamespace(
                    ord_id=10,
                    user_id=20,
                    latest_stat="MFG",
                    company_name="회사",
                    customer_name="담당자",
                    total_amount=1000.0,
                    requested_delivery="2026-07-30",
                    confirmed_delivery="2026-07-30",
                    created_at="2026-07-23T09:00:00",
                    prod_id=3,
                )
            ],
            "patterns": [
                SimpleNamespace(
                    ord_id=10,
                    pattern_id=3,
                    ptn_loc_id=7,
                )
            ],
            "stages": [
                SimpleNamespace(
                    zone_id=1,
                    zone_nm="MOLD",
                    in_progress_count=2,
                )
            ],
            "items": [
                SimpleNamespace(
                    item_id=30,
                    ord_id=10,
                    flow_stat="CAST",
                    zone_nm="MOLD",
                    result=None,
                    cur_stat="MM",
                    cur_res="MM01",
                    is_defective=None,
                    updated_at=datetime(2026, 7, 23, 10, 0, 0),
                )
            ],
        }


class _OperationsRpc(OperationsRpcMixin):
    operations_query_service = _OperationsServiceFake()


class _Request:
    hours = 24


def test_query_service_builds_all_periodic_sections(monkeypatch) -> None:
    session = _SessionContextFake()
    item_service = _ItemQueryServiceFake()
    pattern_service = _PatternQueryServiceFake()
    order_service = _ProductionOrderQueryServiceFake()
    trace: list[tuple] = []

    monkeypatch.setattr(
        operations_query_service,
        "get_inspection_summary",
        lambda db: trace.append(("summary", db)) or ["summary"],
    )
    monkeypatch.setattr(
        operations_query_service,
        "hourly_item_production",
        lambda db, hours: trace.append(("hourly", db, hours)) or ["hourly"],
    )
    monkeypatch.setattr(
        operations_query_service,
        "err_log_trend",
        lambda db, hours: trace.append(("trend", db, hours)) or ["trend"],
    )
    monkeypatch.setattr(
        operations_query_service,
        "get_dashboard_stats",
        lambda db: trace.append(("dashboard", db)) or {"value": 1},
    )
    service = OperationsQueryService(
        session_factory=lambda: session,
        item_query_service=item_service,
        pattern_query_service=pattern_service,
        production_order_query_service=order_service,
    )

    result = service.get_snapshot(hours=12)

    assert result == {
        "summary": ["summary"],
        "hourly": ["hourly"],
        "err_trend": ["trend"],
        "dashboard": {"value": 1},
        "orders": ["order"],
        "patterns": ["pattern"],
        "stages": ["stage"],
        "items": ["item"],
    }
    assert trace == [
        ("summary", session),
        ("hourly", session, 12),
        ("trend", session, 12),
        ("dashboard", session),
    ]
    assert order_service.calls == [("list_orders", ["MFG"], 200)]
    assert pattern_service.calls == [("list_patterns",)]
    assert item_service.calls == [
        ("list_stages",),
        ("list_items", None, None, 200),
    ]


def test_operations_rpc_maps_all_snapshot_sections() -> None:
    response = _OperationsRpc().GetOperationsSnapshot(
        _Request(),
        context=None,
    )

    assert response.summary[0].pending_count == 1
    assert response.hourly[0].count == 3
    assert response.err_trend[0].equip == 2
    assert response.err_trend[0].trans == 0
    assert response.err_trend[1].trans == 1
    assert response.dashboard["total_orders"] == "5"
    assert response.dashboard["timescaledb_enabled"] == "False"
    assert response.orders[0].ord_id == 10
    assert response.orders[0].prod_id == 3
    assert response.patterns[0].ptn_loc_id == 7
    assert response.stages[0].zone_nm == "MOLD"
    assert response.items[0].id == 30
    assert response.items[0].cur_stat == "MM"
