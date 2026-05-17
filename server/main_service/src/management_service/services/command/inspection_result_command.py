"""검사 결과 DB 영속화 — insp_task_txn 종결 + ai_inference_txn + insp_stat + item.

AI 서버 응답을 받은 직후 호출되어 단일 트랜잭션 내에서:
    1. insp_task_txn PROC → SUCC 전이 + result + end_at
    2. ai_inference_txn INSERT (model_id, step_type, txn_stat=SUCC, start/end_at)
    3. insp_stat INSERT (predicted_class, yolo_confidence, anomaly_score, ...)
    4. item.is_defective 갱신

모든 단계가 한 commit 으로 묶이므로 부분 성공이 없다. 실패 시 ROLLBACK.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from smart_cast_db.database import SessionLocal
from smart_cast_db.models import AiInferenceTxn, AiModel, InspStat, InspTaskTxn, Item

logger = logging.getLogger(__name__)

_AI_INFERENCE_STEP_TYPE = "CLASSIFICATION"


@dataclass(frozen=True)
class InspectionResultRow:
    """기록 결과 projection — caller 가 후속 task lifecycle 진행 시 사용."""

    insp_txn_id: int
    item_id: int
    inference_id: int
    model_id: int
    result: str  # "OK" | "NG"
    is_defective: bool
    predicted_class: str | None
    recorded_at: datetime


class InspectionResultCommand:
    """검사 결과 한 사이클 분 DB 기록."""

    def record_inspection_result(
        self,
        *,
        item_id: int,
        is_defective: bool,
        predicted_class: str | None = None,
        yolo_confidence: float | None = None,
        anomaly_score: float | None = None,
        anomaly_threshold: float | None = None,
        model_id: int | None = None,
        model_type: str | None = None,
        step_type: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        raw_inference_payload: dict[str, Any] | None = None,
        segmented_image_b64: str | None = None,
        result_image_b64: str | None = None,
    ) -> InspectionResultRow:
        """PROC insp_task_txn → SUCC 종결 + 부속 결과 영속화.

        Raises:
            LookupError: 활성 PROC insp_task_txn 가 없거나 활성 ai_model 이 없을 때
        """
        if item_id <= 0:
            raise ValueError(f"invalid item_id={item_id}")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        started_at = started_at or now
        completed_at = completed_at or now

        with SessionLocal() as db:
            insp_row: InspTaskTxn | None = (
                db.query(InspTaskTxn)
                .filter(InspTaskTxn.item_id == item_id)
                .filter(InspTaskTxn.txn_stat == "PROC")
                .order_by(InspTaskTxn.req_at.desc(), InspTaskTxn.txn_id.desc())
                .with_for_update(of=InspTaskTxn)
                .first()
            )
            if insp_row is None:
                raise LookupError(
                    f"active PROC insp_task_txn not found for item_id={item_id}"
                )

            resolved_model_id = _resolve_model_id(db, model_id, model_type)

            insp_row.txn_stat = "SUCC"
            insp_row.result = not is_defective
            insp_row.end_at = completed_at
            if insp_row.start_at is None:
                insp_row.start_at = started_at

            # AI 응답 base64 이미지 → backend 디스크 저장 + 외부 fetch URL 합성.
            # 디스크 쓰기 실패 / base64 디코드 실패 시 URL 은 None — 영속화는 NULL 로 진행.
            saved_images = _save_result_images(
                item_id=item_id,
                insp_txn_id=insp_row.txn_id,
                segmented_image_b64=segmented_image_b64,
                result_image_b64=result_image_b64,
            )

            # 옵션 B 신 스키마: 추론 메타(class/score/threshold/is_anomaly/result_json)는 ai_inference_txn 으로 이동.
            inference = AiInferenceTxn(
                insp_txn_id=insp_row.txn_id,
                model_id=resolved_model_id,
                step_type=(step_type or _AI_INFERENCE_STEP_TYPE).upper(),
                txn_stat="SUCC",
                predicted_class=_normalize_class(predicted_class),
                confidence=yolo_confidence,
                anomaly_score=anomaly_score,
                anomaly_threshold=anomaly_threshold,
                is_anomaly=is_defective,
                result_json=raw_inference_payload or None,
                segmented_image_url=saved_images.segmented_url,
                result_image_url=saved_images.result_url,
                start_at=started_at,
                end_at=completed_at,
            )
            db.add(inference)
            db.flush()  # inference_id 확보

            final_result = "DP" if is_defective else "GP"
            # model_type 으로 yolo / patchcore FK 분기. PatchCore 흐름(옵션 B)은 patchcore_inference_id.
            is_patchcore = (model_type or "").strip().upper() == "PATCHCORE"
            insp_stat = InspStat(
                insp_txn_id=insp_row.txn_id,
                item_id=item_id,
                yolo_inference_id=None if is_patchcore else inference.inference_id,
                patchcore_inference_id=inference.inference_id if is_patchcore else None,
                final_result=final_result,
                updated_at=completed_at,
            )
            db.merge(insp_stat)

            # insp_task_txn.final_inference_id 도 갱신 — 추적성 강화
            insp_row.final_inference_id = inference.inference_id

            item: Item | None = db.get(Item, item_id)
            if item is not None:
                item.is_defective = is_defective
                item.updated_at = completed_at
            else:
                logger.warning(
                    "InspectionResultCommand.record_inspection_result: item 행 미존재 item_id=%d "
                    "→ is_defective 갱신 skip (검사 결과는 저장됨)",
                    item_id,
                )

            db.commit()

            logger.info(
                "InspectionResultCommand: item_id=%d insp_txn=%d inference=%d result=%s class=%s",
                item_id,
                insp_row.txn_id,
                inference.inference_id,
                final_result,
                predicted_class or "<none>",
            )
            return InspectionResultRow(
                insp_txn_id=insp_row.txn_id,
                item_id=item_id,
                inference_id=inference.inference_id,
                model_id=resolved_model_id,
                result="NG" if is_defective else "OK",
                is_defective=is_defective,
                predicted_class=_normalize_class(predicted_class),
                recorded_at=completed_at,
            )

    def record_inspection_failure(self, *, item_id: int, reason: str) -> None:
        """AI 서버 호출 실패 시 insp_task_txn 만 FAIL 로 종결.

        ai_inference_txn / insp_stat / item.is_defective 는 손대지 않는다 — 추론 결과가
        없으므로. 다음 cycle 진입은 ToPAWait/CONV_ALLOW_MOVE 가 별도 채널로 트리거.
        """
        if item_id <= 0:
            return
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with SessionLocal() as db:
            insp_row: InspTaskTxn | None = (
                db.query(InspTaskTxn)
                .filter(InspTaskTxn.item_id == item_id)
                .filter(InspTaskTxn.txn_stat == "PROC")
                .order_by(InspTaskTxn.req_at.desc(), InspTaskTxn.txn_id.desc())
                .first()
            )
            if insp_row is None:
                logger.warning(
                    "InspectionResultCommand.record_inspection_failure: PROC 미존재 "
                    "item_id=%d reason=%s",
                    item_id,
                    reason,
                )
                return
            insp_row.txn_stat = "FAIL"
            insp_row.end_at = now
            if insp_row.start_at is None:
                insp_row.start_at = now
            db.commit()
            logger.warning(
                "InspectionResultCommand.record_inspection_failure: item_id=%d "
                "insp_txn=%d FAIL reason=%s",
                item_id,
                insp_row.txn_id,
                reason,
            )


def _resolve_model_id(db, model_id: int | None, model_type: str | None) -> int:
    """주어진 model_id 가 ai_model 에 존재하면 그대로 사용, 아니면 활성 모델로 대체."""
    if model_id is not None and model_id > 0:
        exists = db.query(AiModel).filter(AiModel.model_id == model_id).first()
        if exists is not None:
            return model_id
        logger.info(
            "InspectionResultCommand._resolve_model_id: 지정된 model_id=%d 가 DB 에 없음 "
            "→ 활성 모델로 대체",
            model_id,
        )

    q = db.query(AiModel).filter(AiModel.is_active.is_(True))
    if model_type:
        q = q.filter(AiModel.model_type == model_type)
    active = q.order_by(AiModel.model_id.asc()).first()
    if active is None:
        raise LookupError(
            f"no active ai_model row (requested model_id={model_id} type={model_type})"
        )
    return int(active.model_id)


def _normalize_class(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().upper()
    if value in ("CMH", "RMH", "EMH"):
        return value
    return None


def _save_result_images(
    *,
    item_id: int,
    insp_txn_id: int,
    segmented_image_b64: str | None,
    result_image_b64: str | None,
):
    """AiResultImageSinkCommand wrapper — sink 호출 예외도 흡수.

    URL 두 개가 모두 None 인 빈 결과를 반환하더라도 caller 는 AiInferenceTxn 의 url
    컬럼을 NULL 로 채우면 됨. inference 자체는 SUCC 유지 책임 보장.
    """
    # 지연 import — 모듈 import 시 sink 가 disk root 를 검사하지 않도록 격리.
    from .ai_result_image_sink_command import (
        AiResultImageSinkCommand,
        SavedAiResultImages,
    )

    try:
        sink = AiResultImageSinkCommand()
        return sink.save(
            item_id=item_id,
            insp_txn_id=insp_txn_id,
            segmented_image_b64=segmented_image_b64,
            result_image_b64=result_image_b64,
        )
    except Exception as exc:  # noqa: BLE001 — sink 실패는 inference SUCC 유지 정책
        logger.warning(
            "InspectionResultCommand: _save_result_images 예외 item_id=%d insp_txn=%d exc=%s",
            item_id, insp_txn_id, exc,
        )
        return SavedAiResultImages(
            item_id=item_id,
            insp_txn_id=insp_txn_id,
            segmented_path=None,
            segmented_url=None,
            result_path=None,
            result_url=None,
        )
