"""Read-side schedule queries for Management gRPC handlers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import desc

from smart_cast_db.database import SessionLocal
from smart_cast_db.models import EquipTaskTxn, Item, Ord, OrdPattern, OrdStat


@dataclass(frozen=True)
class ScheduleFactorQueryRow:
    name: str
    score: float
    max_score: float
    detail: str


@dataclass(frozen=True)
class SchedulePriorityResultQueryRow:
    order_id: str
    company_name: str
    product_summary: str
    total_quantity: int
    requested_delivery: str
    total_score: float
    rank: int
    factors: list[ScheduleFactorQueryRow]
    recommendation_reason: str
    delay_risk: str
    ready_status: str
    blocking_reasons: list[str]
    estimated_days: int


@dataclass(frozen=True)
class ScheduleJobQueryRow:
    id: str
    order_id: str
    priority_score: float
    priority_rank: int
    assigned_stage: str
    status: str
    estimated_completion: str
    started_at: str
    completed_at: str
    created_at: str
    company_name: str
    customer_name: str
    total_amount: float
    requested_delivery: str
    confirmed_delivery: str
    order_status: str


class ScheduleQueryService:
    """Read-only schedule projections and dry-run priority calculation."""

    def list_jobs(self) -> list[ScheduleJobQueryRow]:
        with SessionLocal() as db:
            rows: list[ScheduleJobQueryRow] = []
            rank = 1
            for ord_obj in db.query(Ord).order_by(desc(Ord.created_at), desc(Ord.ord_id)).all():
                if not self._is_schedule_queue_candidate(db, ord_obj.ord_id):
                    continue
                rows.append(self._virtual_production_job(db, ord_obj, rank=rank))
                rank += 1
            return rows

    def calculate_priority(self, order_ids: list[str]) -> list[SchedulePriorityResultQueryRow]:
        if not order_ids:
            raise ValueError("order_ids is required")
        parsed_ids = [self._parse_ord_id(raw) for raw in order_ids]
        with SessionLocal() as db:
            orders = db.query(Ord).filter(Ord.ord_id.in_(parsed_ids)).all()
            if not orders:
                raise LookupError("selected orders not found")
            # MFG 큐 필터를 적용하지 않음 — 우선순위 계산은 APPR + 패턴 등록된
            # 주문 중 operator 가 선택한 건에 대해 수행하므로 호출자가 이미 필터링함.
            results = [self._priority_result(db, ord_obj) for ord_obj in orders]
            results.sort(key=lambda row: row.total_score, reverse=True)
            return [
                SchedulePriorityResultQueryRow(
                    order_id=row.order_id,
                    company_name=row.company_name,
                    product_summary=row.product_summary,
                    total_quantity=row.total_quantity,
                    requested_delivery=row.requested_delivery,
                    total_score=row.total_score,
                    rank=index,
                    factors=row.factors,
                    recommendation_reason=row.recommendation_reason,
                    delay_risk=row.delay_risk,
                    ready_status=row.ready_status,
                    blocking_reasons=row.blocking_reasons,
                    estimated_days=row.estimated_days,
                )
                for index, row in enumerate(results, start=1)
            ]

    @staticmethod
    def _parse_ord_id(raw: str | int) -> int:
        text = str(raw).strip()
        if text.startswith("ord_"):
            text = text[4:]
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError(f"invalid order_id: {raw}") from exc
        if value <= 0:
            raise ValueError(f"invalid order_id: {raw}")
        return value

    @staticmethod
    def _latest_ord_stat(db, ord_id: int) -> str:
        latest = (
            db.query(OrdStat)
            .filter(OrdStat.ord_id == ord_id)
            .order_by(desc(OrdStat.updated_at), desc(OrdStat.stat_id))
            .first()
        )
        return latest.ord_stat if latest and latest.ord_stat else "RCVD"

    def _is_schedule_queue_candidate(self, db, ord_id: int) -> bool:
        if self._latest_ord_stat(db, ord_id) != "MFG":
            return False
        has_item = db.query(Item.item_id).filter(Item.ord_id == ord_id).first() is not None
        if has_item:
            return False
        has_equip_txn = (
            db.query(EquipTaskTxn.txn_id)
            .join(Item, Item.item_id == EquipTaskTxn.item_id)
            .filter(Item.ord_id == ord_id)
            .first()
            is not None
        )
        return not has_equip_txn

    def _virtual_production_job(self, db, ord_obj: Ord, *, rank: int) -> ScheduleJobQueryRow:
        latest = self._latest_ord_stat(db, ord_obj.ord_id)
        detail = ord_obj.detail
        user = ord_obj.user
        qty = int(detail.qty or 0) if detail else 0
        est_days = 3 + (qty // 50)
        created_at = ord_obj.created_at.isoformat() if ord_obj.created_at else datetime.utcnow().isoformat()
        due_date = detail.due_date.isoformat() if detail and detail.due_date else ""
        estimated_completion = due_date or (datetime.utcnow() + timedelta(days=est_days)).isoformat()
        return ScheduleJobQueryRow(
            id=f"PJ-ORD-{ord_obj.ord_id}",
            order_id=str(ord_obj.ord_id),
            priority_score=0.0,
            priority_rank=rank,
            assigned_stage="production-planning",
            status="queued" if latest in {"APPR", "MFG"} else "completed",
            estimated_completion=estimated_completion,
            started_at="",
            completed_at="",
            created_at=created_at,
            company_name=user.co_nm if user and user.co_nm else "-",
            customer_name=user.user_nm if user and user.user_nm else "-",
            total_amount=float(detail.final_price or 0) if detail else 0.0,
            requested_delivery=due_date,
            confirmed_delivery=due_date,
            order_status="in_production" if latest == "MFG" else "approved",
        )

    def _priority_result(self, db, ord_obj: Ord) -> SchedulePriorityResultQueryRow:
        detail = ord_obj.detail
        user = ord_obj.user
        qty = int(detail.qty or 0) if detail else 0
        due_date = detail.due_date if detail and detail.due_date else None
        days_left = (due_date - datetime.utcnow().date()).days if due_date else 999

        delivery_score = 25.0 if days_left <= 3 else 20.0 if days_left <= 7 else 15.0 if days_left <= 14 else 10.0
        appr_txn = next((t for t in ord_obj.txns if t.txn_type == "APPR"), None)
        appr_at = appr_txn.txn_at if appr_txn else ord_obj.created_at
        age_days = (datetime.utcnow() - appr_at.replace(tzinfo=None)).days if appr_at else 0
        age_score = min(15.0, 5.0 + age_days * 2.0)
        qty_score = min(10.0, max(3.0, qty / 5.0))
        amount = float(detail.final_price or 0) if detail else 0.0
        amount_score = 10.0 if amount >= 1_000_000 else 7.0 if amount >= 500_000 else 5.0
        estimated_days = 3 + (qty // 50)
        buffer_days = days_left - estimated_days
        delay_score = 15.0 if buffer_days <= 0 else 10.0 if buffer_days <= 3 else 5.0
        pattern_row = db.get(OrdPattern, ord_obj.ord_id)
        has_pattern = pattern_row is not None and pattern_row.ptn_loc_id is not None
        ready_score = 20.0 if has_pattern else 8.0
        blocking = [] if has_pattern else ["패턴 위치 미등록: 운영관리 페이지에서 패턴 등록 후 실제 생산 시작 가능"]
        factors = [
            ScheduleFactorQueryRow(
                name="납기일 긴급도",
                score=delivery_score,
                max_score=25.0,
                detail=f"납기 D-{days_left}" if due_date else "납기 미지정",
            ),
            ScheduleFactorQueryRow(
                name="착수 가능 여부",
                score=ready_score,
                max_score=20.0,
                detail="패턴 등록 완료" if has_pattern else "패턴 미등록",
            ),
            ScheduleFactorQueryRow(
                name="주문 체류 시간",
                score=age_score,
                max_score=15.0,
                detail=f"승인 후 {age_days}일",
            ),
            ScheduleFactorQueryRow(
                name="지연 위험도",
                score=delay_score,
                max_score=15.0,
                detail=f"버퍼 {buffer_days}일 (예상 {estimated_days}일 소요)",
            ),
            ScheduleFactorQueryRow(
                name="주문 금액",
                score=amount_score,
                max_score=10.0,
                detail=f"{amount:,.0f}원",
            ),
            ScheduleFactorQueryRow(
                name="수량 효율",
                score=qty_score,
                max_score=10.0,
                detail=f"수량 {qty}",
            ),
        ]
        product_summary = "주물 제품"
        if detail:
            parts = [detail.material, detail.load_class]
            if not any(parts):
                product = getattr(detail, "product", None)
                parts = [
                    getattr(product, "cate_cd", None) if product is not None else None,
                    f"prod:{detail.prod_id}" if detail.prod_id is not None else None,
                ]
            product_summary = " / ".join(str(value) for value in parts if value) or product_summary
        total_score = round(sum(factor.score for factor in factors), 1)
        delay_risk = "high" if days_left <= 3 else "medium" if days_left <= 7 else "low"
        return SchedulePriorityResultQueryRow(
            order_id=str(ord_obj.ord_id),
            company_name=user.co_nm if user and user.co_nm else "-",
            product_summary=product_summary,
            total_quantity=qty,
            requested_delivery=due_date.isoformat() if due_date else "",
            total_score=total_score,
            rank=1,
            factors=factors,
            recommendation_reason=f"납기/수량/금액 기준 점수 {total_score:.1f}.",
            delay_risk=delay_risk,
            ready_status="ready" if has_pattern else "not_ready",
            blocking_reasons=blocking,
            estimated_days=estimated_days,
        )
