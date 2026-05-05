"""Read-side production order queries for Management gRPC handlers."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import desc

from smart_cast_db.database import SessionLocal
from smart_cast_db.models import Ord, OrdStat


@dataclass(frozen=True)
class ProductionOrderQueryRow:
    ord_id: int
    user_id: int
    latest_stat: str
    company_name: str
    customer_name: str
    total_amount: float
    requested_delivery: str
    confirmed_delivery: str
    created_at: str


class ProductionOrderQueryService:
    """Read-only production order projection for PyQt production pages."""

    def list_orders(
        self,
        *,
        status_filters: list[str] | None = None,
        limit: int = 200,
    ) -> list[ProductionOrderQueryRow]:
        normalized_filters = {
            str(value or "").strip().upper() for value in (status_filters or []) if str(value or "").strip()
        }
        with SessionLocal() as db:
            orders = db.query(Ord).order_by(desc(Ord.created_at), desc(Ord.ord_id)).limit(limit).all()
            rows: list[ProductionOrderQueryRow] = []
            for ord_row in orders:
                latest_stat = self._latest_status(db, ord_row.ord_id)
                if normalized_filters and latest_stat not in normalized_filters:
                    continue
                detail = ord_row.detail
                user = ord_row.user
                due_date = detail.due_date.isoformat() if detail and detail.due_date else ""
                rows.append(
                    ProductionOrderQueryRow(
                        ord_id=ord_row.ord_id,
                        user_id=ord_row.user_id,
                        latest_stat=latest_stat,
                        company_name=user.co_nm if user and user.co_nm else "-",
                        customer_name=user.user_nm if user and user.user_nm else "-",
                        total_amount=float(detail.final_price or 0) if detail else 0.0,
                        requested_delivery=due_date,
                        confirmed_delivery=due_date,
                        created_at=ord_row.created_at.isoformat() if ord_row.created_at else "",
                    )
                )
            return rows

    @staticmethod
    def _latest_status(db, ord_id: int) -> str:
        latest = (
            db.query(OrdStat)
            .filter(OrdStat.ord_id == ord_id)
            .order_by(desc(OrdStat.updated_at), desc(OrdStat.stat_id))
            .first()
        )
        return latest.ord_stat if latest and latest.ord_stat else "RCVD"
