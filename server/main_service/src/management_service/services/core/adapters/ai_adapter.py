from __future__ import annotations

import json
import logging
from pathlib import Path

from services.command.ai_inference_command import AiInferenceCommand, AiInferenceResult
from services.contracts.models import AdapterResult

logger = logging.getLogger(__name__)

_VALID_CATE_CDS: tuple[str, ...] = ("CMH", "RMH", "EMH")


class AIAdapter:
    """AI 추론 어댑터 — adapter_router 의 `AI_INFERENCE_REQUEST` 진입점.

    옵션 B 합의 (2026-05-14): AI 서버는 multipart `file` + `model=cate_cd` 로 호출되며
    cate_cd 는 item → ord_detail → product.cate_cd 로 동적 도출한다.
    """

    def __init__(
        self,
        ai_command: AiInferenceCommand | None = None,
    ) -> None:
        self._ai_command = ai_command or AiInferenceCommand()

    def execute(
        self,
        item_id: int,
        _robot_id: str,
        _command: str,
        payload: bytes,
    ) -> AdapterResult:
        try:
            payload_dict = json.loads(payload.decode("utf-8")) if payload else {}
        except json.JSONDecodeError:
            return AdapterResult(success=False, message="invalid_json_payload")

        image_path = payload_dict.get("image_path")
        image_url = payload_dict.get("image_url")
        if not image_path and not image_url:
            return AdapterResult(success=False, message="image_path_or_url_required")
        if not image_path:
            # image_url 직접 전송 경로는 아직 미지원 — AI팀 인터페이스 합의 후 확장
            return AdapterResult(
                success=False,
                message="image_url_passthrough_not_supported",
            )

        local_path = Path(image_path)
        if not local_path.exists():
            return AdapterResult(success=False, message=f"image_path_not_found:{image_path}")

        # product.cate_cd 조회 — PatchCore 모델 라우팅 키 ("CMH" | "RMH" | "EMH")
        try:
            cate_cd = self._resolve_cate_cd(item_id)
        except Exception as exc:  # noqa: BLE001 — DB 예외도 inference 실패 path 로 일관 처리
            logger.warning(
                "AIAdapter._resolve_cate_cd 실패 item_id=%d exc=%s", item_id, exc,
            )
            return self._handle_inference_failure(
                item_id,
                payload_dict,
                AiInferenceResult(
                    ok=False,
                    item_id=item_id,
                    error_reason=f"cate_cd_lookup_failed:{exc}",
                ),
            )
        if cate_cd not in _VALID_CATE_CDS:
            return self._handle_inference_failure(
                item_id,
                payload_dict,
                AiInferenceResult(
                    ok=False,
                    item_id=item_id,
                    error_reason=f"cate_cd_invalid_or_missing:{cate_cd!r} (item_id={item_id})",
                ),
            )

        # AI 서버 호출 — 추론 결과만 반환. DB 갱신은 호출자가 state_manager 에 위임.
        inference = self._ai_command.upload_and_infer(
            image_path=local_path,
            item_id=item_id,
            model=cate_cd,
        )
        if not inference.ok or inference.is_defective is None:
            return self._handle_inference_failure(item_id, payload_dict, inference)

        response_payload = {
            **payload_dict,
            "inference": _inference_to_dict(inference),
        }
        return AdapterResult(
            success=True,
            message="ai_inference_completed",
            payload=response_payload,
        )

    def close(self) -> None:
        pass

    # ---------- internal ----------------------------------------------------
    def _resolve_cate_cd(self, item_id: int) -> str | None:
        """item_id → ord_detail → product.cate_cd 단일 조회 (smartcast 스키마).

        Returns:
            "CMH" | "RMH" | "EMH" 중 하나, 또는 None (행이 없거나 cate_cd=NULL).
        """
        if item_id <= 0:
            return None
        from smart_cast_db.database import SessionLocal
        from smart_cast_db.models import Item, OrdDetail, Product

        with SessionLocal() as db:
            row = (
                db.query(Product.cate_cd)
                .join(OrdDetail, OrdDetail.prod_id == Product.prod_id)
                .join(Item, Item.ord_id == OrdDetail.ord_id)
                .filter(Item.item_id == item_id)
                .first()
            )
            if row is None or row[0] is None:
                return None
            value = str(row[0]).strip().upper()
            return value or None

    def _handle_inference_failure(
        self,
        item_id: int,
        payload_dict: dict,
        inference: AiInferenceResult,
    ) -> AdapterResult:
        reason = inference.error_reason or "ai_inference_failed"
        logger.warning(
            "AIAdapter._handle_inference_failure: item_id=%d reason=%s — 호출자에게 위임",
            item_id, reason,
        )
        return AdapterResult(
            success=False,
            message=reason,
            payload={
                **payload_dict,
                "item_id": item_id,
                "inference": {
                    "ok": False,
                    "error_reason": reason,
                    "raw_payload": inference.raw_payload,
                },
                "error_reason": reason,
            },
        )


def _inference_to_dict(inference: AiInferenceResult) -> dict:
    """state_manager.record_inspection_result 가 사용할 추론 결과 dict 형식."""
    return {
        "ok": True,
        "is_defective": bool(inference.is_defective),
        "predicted_class": inference.predicted_class,
        "yolo_confidence": inference.yolo_confidence,
        "anomaly_score": inference.anomaly_score,
        "anomaly_threshold": inference.anomaly_threshold,
        "model_id": inference.model_id,
        "model_type": inference.model_type,
        "step_type": inference.step_type,
        "started_at": inference.started_at,
        "completed_at": inference.completed_at,
        "raw_payload": inference.raw_payload,
    }


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
