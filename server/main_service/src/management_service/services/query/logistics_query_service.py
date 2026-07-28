"""물류와 창고 snapshot용 읽기 전용 조회."""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from smart_cast_db.database import SessionLocal
from smart_cast_db.models import (
    Ord,
    OrdStat,
    StrgLocationStat,
    TransTaskTxn,
)


def list_transport_tasks(
    db: Session,
    *,
    trans_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = db.query(TransTaskTxn)
    if trans_id:
        query = query.filter(TransTaskTxn.res_id == trans_id)
    rows = (
        query.order_by(desc(TransTaskTxn.req_at))
        .limit(max(1, limit))
        .all()
    )
    return [
        {
            "txn_id": int(row.txn_id),
            "res_id": row.res_id or "",
            "trans_task_txn_id": int(row.txn_id),
            "trans_id": row.res_id or "",
            "task_type": row.task_type or "",
            "txn_stat": row.txn_stat or "",
            "chg_loc_id": row.chg_loc_id,
            "item_id": int(row.item_id or 0),
            "ord_id": int(row.ord_id or 0),
            "req_at": row.req_at,
            "start_at": row.start_at,
            "end_at": row.end_at,
        }
        for row in rows
    ]


def list_outbound_orders(db: Session) -> list[dict[str, Any]]:
    rows = (
        db.query(Ord, OrdStat)
        .join(OrdStat, OrdStat.ord_id == Ord.ord_id)
        .filter(OrdStat.ord_stat.in_({"DONE", "SHIPPING", "COMP"}))
        .order_by(desc(OrdStat.updated_at), desc(Ord.ord_id))
        .all()
    )
    return [
        {
            "ord_id": int(order.ord_id),
            "user_id": int(order.user_id),
            "stat": stat.ord_stat or "",
            "updated_at": stat.updated_at,
        }
        for order, stat in rows
    ]


def list_warehouse_locations(db: Session) -> list[dict[str, Any]]:
    rows = (
        db.query(StrgLocationStat)
        .order_by(
            StrgLocationStat.loc_row,
            StrgLocationStat.loc_col,
        )
        .all()
    )
    return [
        {
            "loc_id": str(row.loc_id),
            "row": int(row.loc_row or 0),
            "col": int(row.loc_col or 0),
            "status": row.status or "",
            "item_id": int(row.item_id or 0),
            "stored_at": row.stored_at,
        }
        for row in rows
    ]


class LogisticsQueryService:
    """Management가 소유하는 물류와 창고 조회."""

    def __init__(
        self,
        session_factory=SessionLocal,
    ) -> None:
        self._session_factory = session_factory

    def get_snapshot(self) -> dict[str, Any]:
        with self._session_factory() as db:
            return {
                "tasks": list_transport_tasks(db),
                "orders": list_outbound_orders(db),
            }

    def list_warehouse_locations(self) -> list[dict[str, Any]]:
        with self._session_factory() as db:
            return list_warehouse_locations(db)
