import os
import asyncio
import logging
from contextlib import asynccontextmanager

import torch

from fastapi import FastAPI, File, UploadFile, HTTPException
from anomalib.models import Patchcore

from utils import run_inference
from schemas import PredictResponse, ErrorResponse

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
CHECKPOINT_PATH = os.getenv("CHECKPOINT_PATH", "./checkpoints/model.ckpt")
DEVICE = os.getenv("DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
MODEL_NAME = os.getenv("MODEL", "unknown")
IMG_H = int(os.getenv("IMG_H", "256"))
IMG_W = int(os.getenv("IMG_W", "256"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# State / Lifespan
# ─────────────────────────────────────────────
model: Patchcore = None
model_ready = False
inference_sem = asyncio.Semaphore(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, model_ready

    logger.info(f"Loading model '{MODEL_NAME}' from {CHECKPOINT_PATH} on {DEVICE} (input={IMG_H}x{IMG_W})")
    model = Patchcore.load_from_checkpoint(checkpoint_path=CHECKPOINT_PATH,
                                           weights_only=False,
                                           map_location="cpu").eval().to(DEVICE)

    logger.info("Warming up...")
    with torch.no_grad():
        dummy = torch.zeros(1, 3, IMG_H, IMG_W, device=DEVICE)
        _ = model.model(dummy)

    model_ready = True
    logger.info(f"Model '{MODEL_NAME}' ready")
    yield
    
    model_ready = False
    logger.info(f"Shutting down '{MODEL_NAME}'")


app = FastAPI(title="PatchCore Inference Server", lifespan=lifespan)


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────
@app.post("/predict", 
          response_model=PredictResponse, 
          responses={400: {"model": ErrorResponse, "description": "이미지 파일이 아님"},
                     503: {"model": ErrorResponse, "description": "모델 로딩 전"}},
          summary="이미지 이상탐지 추론",
          description=("단일 이미지를 받아 PatchCore 모델로 이상 여부를 판정합니다.\n\n"
                       "- 입력: multipart/form-data, `file` 필드에 이미지(png/jpg)\n"
                       "- 출력: 라벨, 점수, 시각화 PNG(base64)\n"
                       "- 판정: `pred_score > 0.5` 이면 Anomalous"))    
async def predict(file: UploadFile = File(...)) -> PredictResponse:
    if not model_ready:
        raise HTTPException(503, "model not ready")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "image file required")

    image_bytes = await file.read()
    async with inference_sem:
        result = run_inference(model, image_bytes, IMG_H, IMG_W, DEVICE)

    return PredictResponse(**result)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": MODEL_NAME}


@app.get("/readyz")
def readyz():
    if not model_ready:
        raise HTTPException(503, "model not loaded yet")
    return {"status": "ready", "model": MODEL_NAME, "device": DEVICE, "input_size": [IMG_H, IMG_W]}