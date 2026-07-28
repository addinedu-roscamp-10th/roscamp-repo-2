"""FastAPI mock AI service — 옵션 B 정합 (PatchCore /predict).

엔드포인트:
    GET  /health              → 헬스체크
    POST /predict (multipart) → file + model(cate_cd) 수신 → PredictResponse 응답
    GET  /metrics             → 누적 호출 통계 (debug)

옵션 B 합의 (2026-05-14):
    - 요청: multipart `file` (binary) + `model` (str: CMH/RMH/EMH)
    - 응답 PredictResponse {pred_label, pred_score, segmented_image, result_image}
        * pred_label: "Normal" | "Anomalous"
        * pred_score: 0.0~1.0 (anomaly score)
        * segmented_image / result_image: base64 PNG (mock 은 1px 더미)

환경변수:
    AI_MOCK_MODE        - always_pass | always_fail | round_robin | random (기본 round_robin)
    AI_MOCK_PASS_RATIO  - random 모드 OK(=Normal) 비율 (기본 0.7)
    AI_SERVICE_HOST     - bind host (uvicorn 기본 127.0.0.1)
    AI_SERVICE_PORT     - bind port (uvicorn 기본 30000)
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .mock_engine import MockInferenceEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

_VALID_MODELS: tuple[str, ...] = ("CMH", "RMH", "EMH")

# 1x1 단색 PNG (mock 응답 segmented_image / result_image 용 — 디코드 가능 최소 PNG)
_DUMMY_PNG_B64 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63601000000005000156a3680e0000000049454e44ae426082"
    )
).decode("ascii")


class _Metrics:
    """In-process 호출 카운터 — 디버그/스모크 용."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0
        self._normal = 0
        self._anomalous = 0
        self._last_at: str | None = None

    def record(self, pred_label: str) -> None:
        with self._lock:
            self._total += 1
            if pred_label == "Normal":
                self._normal += 1
            elif pred_label == "Anomalous":
                self._anomalous += 1
            self._last_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total": self._total,
                "normal": self._normal,
                "anomalous": self._anomalous,
                "last_at": self._last_at,
            }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = MockInferenceEngine()
    app.state.metrics = _Metrics()
    logger.info("ai_service mock 시작 완료 (옵션 B /predict)")
    yield
    logger.info("ai_service mock 종료")


app = FastAPI(title="SmartCast Mock AI Service", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-mock", "version": "0.2.0"}


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    return app.state.metrics.snapshot()


@app.post("/predict")
async def predict(
    file: UploadFile = File(..., description="검사 이미지 (JPEG/PNG binary)"),
    model: str = Form(..., description="PatchCore 라우팅 키 — cate_cd (CMH|RMH|EMH)"),
) -> JSONResponse:
    if model not in _VALID_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_model:{model!r} (expected one of CMH/RMH/EMH)",
        )

    try:
        image_bytes = await file.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("predict: image read 실패 model=%s exc=%s", model, exc)
        raise HTTPException(status_code=400, detail="image_read_failed") from exc

    if not image_bytes:
        raise HTTPException(status_code=400, detail="image_bytes_empty")

    started = time.time()
    inference = app.state.engine.infer(item_id=0, started_at=started)
    pred_label = "Anomalous" if inference.is_defective else "Normal"
    app.state.metrics.record(pred_label)

    payload: dict[str, Any] = {
        "pred_label": pred_label,
        "pred_score": round(float(inference.anomaly_score), 4),
        "segmented_image": _DUMMY_PNG_B64,
        "result_image": _DUMMY_PNG_B64,
    }
    logger.info(
        "predict: model=%s bytes=%d pred_label=%s pred_score=%.4f",
        model,
        len(image_bytes),
        pred_label,
        payload["pred_score"],
    )
    return JSONResponse(payload)
