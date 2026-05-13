"""FastAPI mock AI service — 검사 이미지 수신 + 양/불 판정 응답.

엔드포인트:
    GET  /health            → 헬스체크
    POST /infer (multipart) → 이미지 + item_id 수신 → mock 판정 결과 반환
    GET  /metrics           → 누적 호출 통계 (debug)

환경변수:
    AI_INSP_IMAGE_ROOT  - 이미지 저장 루트 (기본 ./Inspection_Image)
    AI_INSP_MAX_FILES   - item_id 폴더당 최대 파일 (기본 50)
    AI_MOCK_MODE        - always_pass | always_fail | round_robin | random (기본 round_robin)
    AI_MOCK_PASS_RATIO  - random 모드 OK 비율 (기본 0.7)
    AI_SERVICE_HOST     - bind host (uvicorn 기본 127.0.0.1)
    AI_SERVICE_PORT     - bind port (uvicorn 기본 8100)
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse

from .mock_engine import MockInferenceEngine
from .storage import InspectionImageStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


class _Metrics:
    """In-process 호출 카운터 — 디버그/스모크 용."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0
        self._ok = 0
        self._ng = 0
        self._last_at: str | None = None

    def record(self, result: str) -> None:
        with self._lock:
            self._total += 1
            if result == "OK":
                self._ok += 1
            elif result == "NG":
                self._ng += 1
            self._last_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total": self._total,
                "ok": self._ok,
                "ng": self._ng,
                "last_at": self._last_at,
            }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = InspectionImageStore()
    app.state.engine = MockInferenceEngine()
    app.state.metrics = _Metrics()
    logger.info("ai_service mock 시작 완료")
    yield
    logger.info("ai_service mock 종료")


app = FastAPI(title="SmartCast Mock AI Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-mock"}


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    return app.state.metrics.snapshot()


@app.post("/infer")
async def infer(
    image: UploadFile = File(..., description="검사 이미지 (JPEG/PNG)"),
    item_id: int = Form(..., description="대상 item.item_id"),
    captured_at: float | None = Form(default=None, description="원본 캡처 시각 (epoch sec)"),
    label: str | None = Form(default=None, description="저장 파일명 부속 라벨"),
) -> JSONResponse:
    if item_id <= 0:
        raise HTTPException(status_code=400, detail="item_id_must_be_positive")
    try:
        image_bytes = await image.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("infer: image read 실패 item_id=%d exc=%s", item_id, exc)
        raise HTTPException(status_code=400, detail="image_read_failed") from exc

    if not image_bytes:
        raise HTTPException(status_code=400, detail="image_bytes_empty")

    started = time.time()
    stored = app.state.store.save(
        item_id=item_id,
        image_bytes=image_bytes,
        captured_at=captured_at,
        label=label,
    )
    inference = app.state.engine.infer(item_id=item_id, started_at=started)
    app.state.metrics.record(inference.result)

    payload: dict[str, Any] = {
        "ok": True,
        "item_id": item_id,
        "saved_path": str(stored.path) if stored else None,
        "saved_size": stored.size_bytes if stored else 0,
        "captured_at": stored.captured_at_iso if stored else None,
        **inference.to_payload(),
    }
    logger.info(
        "infer: item_id=%d result=%s class=%s anomaly=%.3f saved=%s",
        item_id,
        inference.result,
        inference.predicted_class,
        inference.anomaly_score,
        stored.path if stored else "<skipped>",
    )
    return JSONResponse(payload)
