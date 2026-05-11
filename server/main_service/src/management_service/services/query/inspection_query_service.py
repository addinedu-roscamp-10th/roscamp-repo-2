"""Read-only inspection queries — insp_task_txn 활성 진행건 / ai_model 조회."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import desc

from smart_cast_db.database import SessionLocal
from smart_cast_db.models import AiModel, InspTaskTxn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InspTaskTxnQueryRow:
    """검사 작업 트랜잭션 projection — ToF2 진입 시 PROC 로 생성됨."""

    txn_id: int
    item_id: int | None
    res_id: str | None
    txn_stat: str
    result: bool | None
    req_at: datetime | None
    start_at: datetime | None
    end_at: datetime | None


@dataclass(frozen=True)
class AiModelQueryRow:
    """ai_model projection — ai_inference_txn 의 FK 참조용."""

    model_id: int
    model_nm: str
    model_type: str  # "YOLO" | "PATCHCORE"
    target_cls: str | None
    is_active: bool


class InspectionQueryService:
    """검사 흐름 read 경로 — sync ORM (`SessionLocal`)."""

    def find_active_insp_txn(self, item_id: int) -> InspTaskTxnQueryRow | None:
        """item_id 에 대해 가장 최근의 PROC 검사 트랜잭션을 반환.

        Tof2 진입 시 `handoff_pipeline` 이 PROC 한 줄을 만들어 두는 것을 전제로 한다.
        없으면 None → caller 가 fallback (생성 또는 skip) 결정.
        """
        if item_id <= 0:
            return None
        with SessionLocal() as db:
            row = (
                db.query(InspTaskTxn)
                .filter(InspTaskTxn.item_id == item_id)
                .filter(InspTaskTxn.txn_stat == "PROC")
                .order_by(desc(InspTaskTxn.req_at), desc(InspTaskTxn.txn_id))
                .first()
            )
            if row is None:
                logger.info(
                    "InspectionQueryService.find_active_insp_txn: PROC 미존재 item_id=%d",
                    item_id,
                )
                return None
            return _to_insp_row(row)

    def find_active_ai_model(self, model_type: str | None = None) -> AiModelQueryRow | None:
        """is_active=true 인 ai_model 중 첫 번째 (model_type 지정 시 일치) 반환."""
        with SessionLocal() as db:
            q = db.query(AiModel).filter(AiModel.is_active.is_(True))
            if model_type:
                q = q.filter(AiModel.model_type == model_type)
            row = q.order_by(AiModel.model_id.asc()).first()
            if row is None:
                logger.warning(
                    "InspectionQueryService.find_active_ai_model: 활성 모델 없음 type=%s",
                    model_type or "<any>",
                )
                return None
            return AiModelQueryRow(
                model_id=row.model_id,
                model_nm=row.model_nm,
                model_type=row.model_type,
                target_cls=row.target_cls,
                is_active=bool(row.is_active),
            )


def _to_insp_row(row: InspTaskTxn) -> InspTaskTxnQueryRow:
    return InspTaskTxnQueryRow(
        txn_id=row.txn_id,
        item_id=row.item_id,
        res_id=row.res_id,
        txn_stat=row.txn_stat,
        result=row.result,
        req_at=row.req_at,
        start_at=row.start_at,
        end_at=row.end_at,
    )
