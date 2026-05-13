"""AI 추론 어댑터 — 검사 이미지 업로드 + DB 결과 영속화.

AdapterRouter 가 `action=AI_INFERENCE_REQUEST` 일 때 본 어댑터를 호출한다.
execute() 한 번에 다음 단계가 진행된다:

    1. payload 검증 (image_path 또는 image_url + item_id)
    2. AiInferenceCommand → AI 서버 multipart POST → 양/불 판정 결과 수신
    3. InspectionResultCommand → insp_task_txn SUCC + ai_inference_txn / insp_stat INSERT

AI 서버 호출 실패 시: insp_task_txn FAIL 로 마감하고 AdapterResult(success=False) 반환.

INSP_COMPLETED publish 시점에 대한 주석 (2026-05-12):
    이전 버전은 본 execute() 마지막에 INSP_COMPLETED 를 publish 하여 Jetson 측에서
    즉시 컨베이어를 가동시켰다. 그러나 AI 추론 완료 시점에는 다음 공정 AMR 이 아직
    컨베이어 출구(ToINSP pose) 에 도착하지 않은 상태일 수 있어 주물이 빈 위치로
    unloading 되는 문제가 있었다. 이를 해결하기 위해 INSP_COMPLETED publish 는
    ToPAWait task 의 step 3 (`CONV_ALLOW_MOVE`) 에서 실행되도록 conv_adapter 로
    이동되었다. ToPAWait step 2 가 AMR 도착 subtask("tostrg") 를 기다리므로
    AMR 도착 보장 후에만 컨베이어가 RUN 한다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from services.command.ai_inference_command import AiInferenceCommand, AiInferenceResult
from services.command.inspection_result_command import (
    InspectionResultCommand,
    InspectionResultRow,
)
from services.contracts.models import AdapterResult

logger = logging.getLogger(__name__)


class AIAdapter:
    """AI 추론 어댑터 — adapter_router 의 `AI_INFERENCE_REQUEST` 진입점."""

    def __init__(
        self,
        ai_command: AiInferenceCommand | None = None,
        result_command: InspectionResultCommand | None = None,
    ) -> None:
        self._ai_command = ai_command or AiInferenceCommand()
        self._result_command = result_command or InspectionResultCommand()

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

        # 1) AI 서버 호출 ------------------------------------------------------
        inference = self._ai_command.upload_and_infer(
            image_path=local_path,
            item_id=item_id,
            captured_at=captured_at,
            label=label,
        )
        if not inference.ok or inference.is_defective is None:
            return self._handle_inference_failure(item_id, inference)

        # 2) DB 영속화 ---------------------------------------------------------
        try:
            recorded = self._result_command.record_inspection_result(
                item_id=item_id,
                is_defective=bool(inference.is_defective),
                predicted_class=inference.predicted_class,
                yolo_confidence=inference.yolo_confidence,
                anomaly_score=inference.anomaly_score,
                anomaly_threshold=inference.anomaly_threshold,
                model_id=inference.model_id,
                model_type=inference.model_type,
                step_type=inference.step_type,
                started_at=_parse_iso(inference.started_at),
                completed_at=_parse_iso(inference.completed_at),
                raw_inference_payload=inference.raw_payload,
            )
        except (LookupError, ValueError) as exc:
            logger.warning(
                "AIAdapter.execute: DB 기록 실패 item_id=%d exc=%s — INSP_COMPLETED skip",
                item_id,
                exc,
            )
            return AdapterResult(
                success=False,
                message=f"inspection_persist_failed:{exc}",
                payload={**payload_dict, "inference": inference.raw_payload},
            )

        # 3) INSP_COMPLETED publish 는 conv_adapter (ToPAWait/CONV_ALLOW_MOVE) 로 이동 -----
        # AMR 도착 보장 후에만 컨베이어가 RUN 하도록 publish 시점을 늦춤.
        response_payload = {
            **payload_dict,
            "inference": inference.raw_payload,
            "inspection_result": {
                "insp_txn_id": recorded.insp_txn_id,
                "inference_id": recorded.inference_id,
                "model_id": recorded.model_id,
                "result": recorded.result,
                "is_defective": recorded.is_defective,
                "predicted_class": recorded.predicted_class,
                "recorded_at": recorded.recorded_at.isoformat(),
            },
        }
        return AdapterResult(
            success=True,
            message="ai_inference_recorded",
            payload=response_payload,
        )

    def close(self) -> None:
        pass

    # ---------- internal ----------------------------------------------------
    def _handle_inference_failure(
        self,
        item_id: int,
        inference: AiInferenceResult,
    ) -> AdapterResult:
        reason = inference.error_reason or "ai_inference_failed"
        try:
            self._result_command.record_inspection_failure(item_id=item_id, reason=reason)
        except Exception as exc:  # noqa: BLE001 — DB 기록 실패가 호출자 흐름을 막지 않도록 격리
            logger.warning(
                "AIAdapter._handle_inference_failure: record_inspection_failure 실패 "
                "item_id=%d exc=%s",
                item_id,
                exc,
            )
        return AdapterResult(
            success=False,
            message=reason,
            payload={
                "item_id": item_id,
                "inference": inference.raw_payload,
                "error_reason": reason,
            },
        )

def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # 끝 'Z' 는 datetime.fromisoformat 가 거부하므로 변환
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.utcfromtimestamp(float(value))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
