# segmenter_server.py
import io
import os
import base64
import asyncio
import logging
from contextlib import asynccontextmanager

import torch
from PIL import Image
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import JSONResponse, Response

from segmenter import GroundedSAM2

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
DEVICE = os.getenv("DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
GROUNDING_MODEL = os.getenv("GROUNDING_MODEL", "IDEA-Research/grounding-dino-tiny")
SAM2_CHECKPOINT = os.getenv("SAM2_CHECKPOINT", "./checkpoints/sam2.1_hiera_large.pt")
SAM2_CONFIG = os.getenv("SAM2_CONFIG", "configs/sam2.1/sam2.1_hiera_l.yaml")
# warm-up 및 디버그용 기본 prompt (운영 시엔 router가 항상 보내줌)
DEFAULT_PROMPT = os.getenv("TEXT_PROMPT", "white object on green conveyor.")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# State / Lifespan
# ─────────────────────────────────────────────
segmenter: GroundedSAM2 = None
ready = False
gpu_sem = asyncio.Semaphore(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global segmenter, ready

    logger.info(f"Loading GroundedSAM2 on {DEVICE} ...")
    segmenter = GroundedSAM2(
        grounding_model_id=GROUNDING_MODEL,
        sam2_checkpoint=SAM2_CHECKPOINT,
        sam2_config=SAM2_CONFIG,
        device=DEVICE,
    )

    logger.info("Warming up model...")
    dummy = Image.new("RGB", (640, 640), color=(0, 0, 0))
    buf = io.BytesIO()
    dummy.save(buf, format="PNG")
    try:
        _ = segmenter.segment(buf.getvalue(), text_prompt=DEFAULT_PROMPT)
        logger.info("Warm-up done")
    except Exception as e:
        logger.warning(f"Warm-up failed (ignore): {e}")

    ready = True
    logger.info("Preprocessing service ready")
    yield

    ready = False
    logger.info("Shutting down preprocessing service")


app = FastAPI(title="GroundedSAM2 Preprocessing Service", lifespan=lifespan)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
async def _segment(image_bytes: bytes, text_prompt: str) -> bytes:
    """이미지를 segment 해서 RGBA PNG bytes 반환. 미검출 시 422."""
    async with gpu_sem:
        cropped_rgba = segmenter.segment(image_bytes, text_prompt=text_prompt)
    if cropped_rgba is None:
        raise HTTPException(422, "object not detected")
    buf = io.BytesIO()
    Image.fromarray(cropped_rgba, mode="RGBA").save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────
@app.post(
    "/crop",
    summary="이미지에서 객체를 찾아 crop 후 base64 JSON으로 반환 (router 용)",
    responses={
        400: {"description": "이미지 아님"},
        422: {"description": "객체 미검출"},
        503: {"description": "서비스 로딩 전"},
    },
)
async def crop(
    file: UploadFile = File(...),
    product: str = Form(..., description="circle | ellipse | rectangle"),
    text_prompt: str = Form(..., description="GroundingDINO prompt (router가 product별로 결정)"),
):
    if not ready:
        raise HTTPException(503, "service not ready")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "image file required")

    raw = await file.read()
    png = await _segment(raw, text_prompt)
    cropped_b64 = base64.b64encode(png).decode("ascii")
    return JSONResponse({"cropped_image": cropped_b64})


@app.post(
    "/segment",
    summary="(디버그용) crop 결과 PNG 만 반환",
    responses={
        200: {"content": {"image/png": {}}},
        422: {"description": "객체 미검출"},
    },
)
async def segment_only(
    file: UploadFile = File(...),
    text_prompt: str = Form(DEFAULT_PROMPT, description="(디버그용) prompt 직접 지정"),
):
    if not ready:
        raise HTTPException(503, "service not ready")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "image file required")
    raw = await file.read()
    png = await _segment(raw, text_prompt)
    return Response(content=png, media_type="image/png")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    if not ready:
        raise HTTPException(503, "loading")
    return {"status": "ready", "device": DEVICE}