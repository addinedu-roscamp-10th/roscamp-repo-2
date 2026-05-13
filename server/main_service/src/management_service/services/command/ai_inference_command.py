"""AI 서버 추론 요청 커맨드 — multipart POST /infer.

`AIAdapter.execute` 가 본 커맨드를 호출하여 backend disk 에 저장된 검사 이미지를
AI 서버로 forward 하고 양/불 판정 결과를 받는다. AI 서버 인터페이스는
e2e 통합 테스트 동안 mock (`server/ai_service`) 가 응답한다.

설계 원칙:
    - sync httpx — `AIAdapter.execute` 가 `asyncio.to_thread` 로 래핑됨
    - 1 회 retry — 일시적 네트워크 오류 흡수, 그 이상은 실패로 보고
    - 응답 schema 는 DB (`insp_stat`, `ai_inference_txn`) 와 정합되도록 정규화
    - timeout 초과 / connection error 시 ok=False 반환 (예외 throw 하지 않음)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8100
_DEFAULT_TIMEOUT_SEC = 5.0
_DEFAULT_RETRY_COUNT = 1
_DEFAULT_PATH = "/infer"


@dataclass(frozen=True)
class AiInferenceResult:
    """AI 서버 추론 응답의 정규화 형태.

    실패 시 ok=False + error_reason 채워서 반환. 호출자는 ok 만 보고 분기.
    성공 시 result/is_defective + DB 필드 (predicted_class, ...) 가 모두 채워짐.
    """

    ok: bool
    item_id: int
    result: str = ""  # "OK" | "NG" | ""
    is_defective: bool | None = None
    predicted_class: str | None = None
    yolo_confidence: float | None = None
    anomaly_score: float | None = None
    anomaly_threshold: float | None = None
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
        captured_at: float | None = None,
        label: str | None = None,
    ) -> AiInferenceResult:
        if item_id <= 0:
            return AiInferenceResult(ok=False, item_id=item_id, error_reason="invalid_item_id")
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

        attempts = max(1, self._retry_count + 1)
        last_error: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._post(image_bytes, image_path.name, item_id, captured_at, label)
            except httpx.HTTPError as exc:
                last_error = f"http_error:{type(exc).__name__}:{exc}"
                logger.warning(
                    "AiInferenceCommand.upload_and_infer: attempt %d/%d 실패 — %s",
                    attempt,
                    attempts,
                    last_error,
                )
                continue
            return _normalize_response(item_id, response)

        return AiInferenceResult(
            ok=False,
            item_id=item_id,
            error_reason=last_error or "ai_endpoint_unreachable",
        )

    def _post(
        self,
        image_bytes: bytes,
        filename: str,
        item_id: int,
        captured_at: float | None,
        label: str | None,
    ) -> httpx.Response:
        files = {"image": (filename, image_bytes, "image/jpeg")}
        data: dict[str, str] = {"item_id": str(item_id)}
        if captured_at is not None:
            data["captured_at"] = f"{captured_at:.6f}"
        if label:
            data["label"] = label
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(self.endpoint, files=files, data=data)
        response.raise_for_status()
        return response


def _normalize_response(item_id: int, response: httpx.Response) -> AiInferenceResult:
    try:
        body = response.json()
    except ValueError:
        return AiInferenceResult(
            ok=False,
            item_id=item_id,
            error_reason="ai_response_not_json",
        )
    if not isinstance(body, dict) or not body.get("ok", False):
        return AiInferenceResult(
            ok=False,
            item_id=item_id,
            error_reason=str(body.get("detail") or body.get("error") or "ai_response_not_ok"),
            raw_payload=body if isinstance(body, dict) else {},
        )

    result = str(body.get("result") or "").upper()
    is_defective_field = body.get("is_defective")
    is_defective: bool | None
    if isinstance(is_defective_field, bool):
        is_defective = is_defective_field
    elif result in ("OK", "NG"):
        is_defective = result == "NG"
    else:
        is_defective = None

    return AiInferenceResult(
        ok=True,
        item_id=int(body.get("item_id") or item_id),
        result=result,
        is_defective=is_defective,
        predicted_class=body.get("predicted_class"),
        yolo_confidence=_as_float(body.get("yolo_confidence")),
        anomaly_score=_as_float(body.get("anomaly_score")),
        anomaly_threshold=_as_float(body.get("anomaly_threshold")),
        model_id=_as_int(body.get("model_id")),
        model_nm=body.get("model_nm"),
        model_type=body.get("model_type"),
        step_type=body.get("step_type"),
        started_at=body.get("started_at"),
        completed_at=body.get("completed_at"),
        saved_path=body.get("saved_path"),
        raw_payload=body,
    )


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
