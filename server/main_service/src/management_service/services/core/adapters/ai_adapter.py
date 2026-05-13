from __future__ import annotations

import json
import logging
from pathlib import Path

from services.command.ai_inference_command import AiInferenceCommand, AiInferenceResult
from services.contracts.models import AdapterResult

logger = logging.getLogger(__name__)


class AIAdapter:
    """AI 추론 어댑터 — adapter_router 의 `AI_INFERENCE_REQUEST` 진입점."""

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

        captured_at = _coerce_float(payload_dict.get("captured_at"))
        label = payload_dict.get("label")

        # AI 서버 호출 — 추론 결과만 반환. DB 갱신은 호출자가 state_manager 에 위임.
        inference = self._ai_command.upload_and_infer(
            image_path=local_path,
            item_id=item_id,
            captured_at=captured_at,
            label=label,
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
