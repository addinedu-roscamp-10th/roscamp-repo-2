"""운영 현황 snapshot용 읽기 전용 조회."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from smart_cast_db.database import SessionLocal, engine
from smart_cast_db.models import (
    EquipErrLog,
    Item,
    Ord,
    OrdStat,
    Res,
    SCHEMA,
    TransErrLog,
)

from services.query.item_query_service import ItemQueryService
from services.query.pattern_query_service import PatternQueryService
from services.query.production_order_query_service import (
    ProductionOrderQueryService,
)


def get_inspection_summary(db: Session) -> list[dict[str, Any]]:
    """발주별 전체, 양품, 불량, 미검사 수량 조회."""
    rows = (
        db.query(
            Item.ord_id.label("ord_id"),
            func.count(Item.item_id).label("total_items"),
            func.count(Item.item_id)
            .filter(Item.is_defective.is_(False))
            .label("good_count"),
            func.count(Item.item_id)
            .filter(Item.is_defective.is_(True))
            .label("defective_count"),
        )
        .group_by(Item.ord_id)
        .order_by(Item.ord_id)
        .all()
    )
    return [
        {
            "ord_id": int(row.ord_id),
            "total_items": int(row.total_items or 0),
            "good_count": int(row.good_count or 0),
            "defective_count": int(row.defective_count or 0),
            "pending_count": max(
                0,
                int(row.total_items or 0)
                - int(row.good_count or 0)
                - int(row.defective_count or 0),
            ),
        }
        for row in rows
    ]


def hourly_item_production(
    db: Session,
    hours: int = 24,
) -> list[dict[str, Any]]:
    """최근 N시간의 품목 갱신 건수를 시간대별로 조회."""
    since = datetime.now() - timedelta(hours=max(1, hours))
    if SCHEMA == "smartcast" and has_timescaledb():
        try:
            rows = db.execute(
                text(
                    f"""
                    SELECT bucket, produced
                    FROM {SCHEMA}.item_hourly
                    WHERE bucket >= :since
                    ORDER BY bucket
                    """
                ),
                {"since": since},
            ).all()
        except ProgrammingError:
            db.rollback()
        else:
            if rows:
                return [
                    {
                        "bucket": row.bucket.isoformat(),
                        "produced": int(row.produced or 0),
                    }
                    for row in rows
                ]

    bucket = func.date_trunc("hour", Item.updated_at).label("bucket")
    rows = (
        db.query(
            bucket,
            func.count(Item.item_id).label("produced"),
        )
        .filter(Item.updated_at >= since)
        .group_by(bucket)
        .order_by(bucket)
        .all()
    )
    return [
        {
            "bucket": row.bucket.isoformat(),
            "produced": int(row.produced or 0),
        }
        for row in rows
    ]


def err_log_trend(
    db: Session,
    hours: int = 24,
) -> list[dict[str, Any]]:
    """최근 N시간의 설비, 운송 오류를 시간대별로 조회."""
    since = datetime.now() - timedelta(hours=max(1, hours))

    def _rows(model, source: str) -> list[dict[str, Any]]:
        bucket = func.date_trunc("hour", model.occured_at).label("bucket")
        rows = (
            db.query(
                bucket,
                func.count(model.err_id).label("count"),
            )
            .filter(model.occured_at >= since)
            .group_by(bucket)
            .order_by(bucket)
            .all()
        )
        return [
            {
                "bucket": row.bucket.isoformat(),
                "source": source,
                "count": int(row.count or 0),
            }
            for row in rows
        ]

    result = _rows(EquipErrLog, "equip") + _rows(TransErrLog, "trans")
    result.sort(key=lambda row: (row["bucket"], row["source"]))
    return result


@lru_cache(maxsize=1)
def has_timescaledb() -> bool:
    """현재 DB의 TimescaleDB extension 사용 가능 여부 조회."""
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT extname FROM pg_extension "
                    "WHERE extname = 'timescaledb' LIMIT 1"
                )
            ).first()
            return row is not None
    except Exception:
        return False


def get_dashboard_stats(db: Session) -> dict[str, Any]:
    """운영 화면의 dashboard 통계를 한 묶음으로 조회."""
    latest_stats = {
        int(row.ord_id): row.ord_stat or "RCVD"
        for row in db.query(OrdStat).all()
    }
    total_orders = int(db.query(func.count(Ord.ord_id)).scalar() or 0)
    in_production = sum(
        1 for value in latest_stats.values() if value in {"APPR", "MFG"}
    )
    completed = sum(
        1
        for value in latest_stats.values()
        if value in {"DONE", "SHIPPING", "COMP"}
    )
    pending = sum(1 for value in latest_stats.values() if value == "RCVD")

    total_items = int(db.query(func.count(Item.item_id)).scalar() or 0)
    good_items = int(
        db.query(func.count(Item.item_id))
        .filter(Item.is_defective.is_(False))
        .scalar()
        or 0
    )
    defective_items = int(
        db.query(func.count(Item.item_id))
        .filter(Item.is_defective.is_(True))
        .scalar()
        or 0
    )
    inspected = good_items + defective_items
    defect_rate = defective_items / inspected * 100.0 if inspected else 0.0

    today_start = datetime.now().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    equip_errors = int(
        db.query(func.count(EquipErrLog.err_id))
        .filter(EquipErrLog.occured_at >= today_start)
        .scalar()
        or 0
    )
    trans_errors = int(
        db.query(func.count(TransErrLog.err_id))
        .filter(TransErrLog.occured_at >= today_start)
        .scalar()
        or 0
    )

    return {
        "total_orders": total_orders,
        "orders_in_production": in_production,
        "orders_completed": completed,
        "orders_pending": pending,
        "total_items": total_items,
        "good_items": good_items,
        "defective_items": defective_items,
        "defect_rate_pct": round(defect_rate, 2),
        "alerts_today": equip_errors + trans_errors,
        "active_resources": int(
            db.query(func.count(Res.res_id)).scalar() or 0
        ),
        "snapshot_at": datetime.utcnow().isoformat() + "Z",
        "timescaledb_enabled": has_timescaledb(),
    }


class OperationsQueryService:
    """운영 화면의 주기 갱신 데이터를 한 snapshot으로 조회."""

    def __init__(
        self,
        session_factory=SessionLocal,
        *,
        item_query_service: ItemQueryService | None = None,
        pattern_query_service: PatternQueryService | None = None,
        production_order_query_service: ProductionOrderQueryService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._item_query_service = item_query_service or ItemQueryService()
        self._pattern_query_service = pattern_query_service or PatternQueryService()
        self._production_order_query_service = (
            production_order_query_service or ProductionOrderQueryService()
        )

    def get_snapshot(self, *, hours: int = 24) -> dict[str, Any]:
        normalized_hours = max(1, hours)
        with self._session_factory() as db:
            snapshot = {
                "summary": get_inspection_summary(db),
                "hourly": hourly_item_production(db, hours=normalized_hours),
                "err_trend": err_log_trend(db, hours=normalized_hours),
                "dashboard": get_dashboard_stats(db),
            }

        snapshot.update(
            {
                "orders": self._production_order_query_service.list_orders(
                    status_filters=["MFG"],
                    limit=200,
                ),
                "patterns": self._pattern_query_service.list_patterns(),
                "stages": self._item_query_service.list_stages(),
                "items": self._item_query_service.list_items(
                    order_id=None,
                    stage=None,
                    limit=200,
                ),
            }
        )
        return snapshot
