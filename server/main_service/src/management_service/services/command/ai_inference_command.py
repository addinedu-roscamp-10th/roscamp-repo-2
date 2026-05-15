"""AI 서버 추론 요청 커맨드 — multipart POST /predict (옵션 B 합의 2026-05-14).

PatchCore router 호환:
    - 요청: multipart `file` (검사 이미지 binary) + `model` (cate_cd: CMH/RMH/EMH)
    - 응답: PredictResponse{pred_label, pred_score, segmented_image, result_image}
        - pred_label: "Normal" | "Anomalous"
        - pred_score: anomaly score (float)
        - segmented_image / result_image: base64 PNG 두 장

DB 매핑:
    - pred_label="Anomalous" → is_defective=True, result="NG"
    - pred_score → anomaly_score (insp_stat.anomaly_score)
    - 송신 model → predicted_class (insp_stat.predicted_class)
    - 응답 base64 이미지는 호출자(AIAdapter) 가 디스크 저장 후 경로만 보관
      (raw_payload 에는 길이 마커로만 남겨 DB result_json 크기 폭주 방지)

설계 원칙:
    - sync httpx — `AIAdapter.execute` 가 `asyncio.to_thread` 로 래핑됨
    - 1 회 retry — 일시적 네트워크 오류 흡수, 그 이상은 실패로 보고
    - timeout 초과 / connection error 시 ok=False 반환 (예외 throw 하지 않음)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 옵션 B 합의 endpoint — .env.example 의 MGMT_AI_* 와 정합.
_DEFAULT_HOST = "100.66.177.119"
_DEFAULT_PORT = 30000
_DEFAULT_TIMEOUT_SEC = 30.0
_DEFAULT_RETRY_COUNT = 1
_DEFAULT_PATH = "/predict"

_VALID_MODELS: tuple[str, ...] = ("CMH", "RMH", "EMH")


@dataclass(frozen=True)
class AiInferenceResult:
    """AI 서버 추론 응답의 정규화 형태 (옵션 B + DB 매핑)."""

    ok: bool
    item_id: int
    # ----- 옵션 B 응답 원본 -----
    pred_label: str | None = None
    pred_score: float | None = None
    segmented_image_b64: str | None = None
    result_image_b64: str | None = None
    # ----- DB 매핑 (insp_stat / ai_inference_txn / item) -----
    result: str = ""                       # "OK" | "NG" | ""
    is_defective: bool | None = None
    predicted_class: str | None = None     # = 송신 model (cate_cd)
    yolo_confidence: float | None = None   # 옵션 B 미제공 → None
    anomaly_score: float | None = None     # = pred_score
    anomaly_threshold: float | None = None # 옵션 B 미제공 → None
    model_id: int | None = None
    model_nm: str | None = None
    model_type: str | None = None
    step_type: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    saved_path: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    error_reason: str | None = None


class AiInferenceCommand:
    """AI 서버로 검사 이미지 multipart POST 후 결과 반환."""

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        timeout_sec: float | None = None,
        retry_count: int | None = None,
        path: str | None = None,
    ) -> None:
        self._host = (host or os.environ.get("MGMT_AI_HOST", _DEFAULT_HOST)).strip() or _DEFAULT_HOST
        self._port = int(port if port is not None else os.environ.get("MGMT_AI_PORT", _DEFAULT_PORT))
        try:
            self._timeout = float(
                timeout_sec
                if timeout_sec is not None
                else os.environ.get("MGMT_AI_TIMEOUT_SEC", _DEFAULT_TIMEOUT_SEC)
            )
        except (TypeError, ValueError):
            self._timeout = _DEFAULT_TIMEOUT_SEC
        self._retry_count = int(
            retry_count
            if retry_count is not None
            else os.environ.get("MGMT_AI_RETRY_COUNT", _DEFAULT_RETRY_COUNT)
        )
        self._path = (path or os.environ.get("MGMT_AI_INFER_PATH", _DEFAULT_PATH)) or _DEFAULT_PATH
        logger.info(
            "AiInferenceCommand 초기화: endpoint=http://%s:%d%s timeout=%.1fs retry=%d",
            self._host,
            self._port,
            self._path,
            self._timeout,
            self._retry_count,
        )

    @property
    def endpoint(self) -> str:
        return f"http://{self._host}:{self._port}{self._path}"

    def upload_and_infer(
        self,
        *,
        image_path: Path,
        item_id: int,
        model: str,
    ) -> AiInferenceResult:
        """multipart POST /predict — PatchCore 추론 1 회 호출.

        Args:
            image_path: 디스크에 저장된 검사 이미지 경로
            item_id: insp_task_txn 매칭용 (응답 자체에는 영향 없음, 결과 객체에만 보존)
            model: PatchCore 라우팅 키 — product.cate_cd ("CMH" | "RMH" | "EMH")
        """
        if item_id <= 0:
            return AiInferenceResult(ok=False, item_id=item_id, error_reason="invalid_item_id")
        if model not in _VALID_MODELS:
            return AiInferenceResult(
                ok=False,
                item_id=item_id,
                error_reason=f"invalid_model:{model!r} (expected CMH/RMH/EMH)",
            )
        if not image_path.exists():
            return AiInferenceResult(
                ok=False,
                item_id=item_id,
                error_reason=f"image_path_not_found:{image_path}",
            )

        try:
            image_bytes = image_path.read_bytes()
        except OSError as exc:
            logger.warning(
                "AiInferenceCommand.upload_and_infer: read 실패 path=%s exc=%s",
                image_path,
                exc,
            )
            return AiInferenceResult(
                ok=False,
                item_id=item_id,
                error_reason=f"image_read_failed:{exc}",
            )

        started_iso = _utcnow_iso()
        attempts = max(1, self._retry_count + 1)
        last_error: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._post(image_bytes, image_path.name, model)
            except httpx.HTTPError as exc:
                last_error = f"http_error:{type(exc).__name__}:{exc}"
                logger.warning(
                    "AiInferenceCommand.upload_and_infer: attempt %d/%d 실패 — %s",
                    attempt,
                    attempts,
                    last_error,
                )
                continue
            completed_iso = _utcnow_iso()
            return _normalize_response(
                item_id=item_id,
                model=model,
                response=response,
                started_at=started_iso,
                completed_at=completed_iso,
            )

        return AiInferenceResult(
            ok=False,
            item_id=item_id,
            error_reason=last_error or "ai_endpoint_unreachable",
        )

    def _post(
        self,
        image_bytes: bytes,
        filename: str,
        model: str,
    ) -> httpx.Response:
        # content-type 은 확장자로 추론 (.png → image/png, 그 외 image/jpeg). PatchCore 서버가
        # multipart 본문의 content-type 을 검사해 application/octet-stream 은 502 로 reject 한
        # 사례가 있어 명시적으로 image MIME 을 지정한다.
        mime = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
        files = {"file": (filename, image_bytes, mime)}
        data: dict[str, str] = {"model": model}
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(self.endpoint, files=files, data=data)
        response.raise_for_status()
        return response


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _normalize_response(
    *,
    item_id: int,
    model: str,
    response: httpx.Response,
    started_at: str,
    completed_at: str,
) -> AiInferenceResult:
    try:
        body = response.json()
    except ValueError:
        return AiInferenceResult(
            ok=False,
            item_id=item_id,
            error_reason="ai_response_not_json",
        )
    if not isinstance(body, dict):
        return AiInferenceResult(
            ok=False,
            item_id=item_id,
            error_reason="ai_response_not_dict",
        )

    pred_label_raw = body.get("pred_label")
    pred_score_raw = body.get("pred_score")
    if pred_label_raw is None or pred_score_raw is None:
        return AiInferenceResult(
            ok=False,
            item_id=item_id,
            error_reason=f"ai_response_missing_fields:keys={sorted(body.keys())}",
            raw_payload=_strip_b64(body),
        )

    pred_label = str(pred_label_raw)
    pred_score = _as_float(pred_score_raw)
    is_defective = pred_label.strip().upper() == "ANOMALOUS"
    result = "NG" if is_defective else "OK"

    return AiInferenceResult(
        ok=True,
        item_id=item_id,
        pred_label=pred_label,
        pred_score=pred_score,
        segmented_image_b64=body.get("segmented_image"),
        result_image_b64=body.get("result_image"),
        result=result,
        is_defective=is_defective,
        predicted_class=model,             # 송신 cate_cd 를 그대로 보존 — DB 의 insp_stat.predicted_class 키
        anomaly_score=pred_score,          # 옵션 B pred_score 매핑
        model_type="PATCHCORE",
        step_type="CLASSIFICATION",
        started_at=started_at,
        completed_at=completed_at,
        raw_payload=_strip_b64(body),      # base64 이미지는 raw_payload 에서 제외 (DB result_json 크기 절약)
    )


def _strip_b64(body: dict[str, Any]) -> dict[str, Any]:
    """raw_payload 의 base64 이미지 필드를 길이 마커로 치환 — DB result_json 사이즈 폭주 방지."""
    out: dict[str, Any] = {}
    for k, v in body.items():
        if k in ("segmented_image", "result_image") and isinstance(v, str):
            out[k] = f"<base64 omitted, len={len(v)}>"
        else:
            out[k] = v
    return out


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
