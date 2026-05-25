# router.py
import os
import base64
import logging
import httpx

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
PREPROCESSING_URL = os.getenv("PREPROCESSING_URL", "http://preprocessing-service:8080")

MODEL_ROUTES = {
    "circle":    os.getenv("CIRCLE_URL",    "http://model-circle-service:8080"),
    "ellipse":   os.getenv("ELLIPSE_URL",   "http://model-ellipse-service:8080"),
    "rectangle": os.getenv("RECTANGLE_URL", "http://model-rectangle-service:8080"),
}

# product별 GroundingDINO prompt
# 추후 튜닝 필요 시 이 dict만 수정하면 됨
PRODUCT_PROMPTS = {
    "circle":    os.getenv("CIRCLE_PROMPT",    "circular white plate on green conveyor."),
    "ellipse":   os.getenv("ELLIPSE_PROMPT",   "elliptical white object on green conveyor."),
    "rectangle": os.getenv("RECTANGLE_PROMPT", "rectangular white tray on green conveyor."),
}

PREP_TIMEOUT = httpx.Timeout(30.0, connect=2.0)
MODEL_TIMEOUT = httpx.Timeout(15.0, connect=2.0)

app = FastAPI(title="AI Router")


# ─────────────────────────────────────────────
# Pipeline steps
# ─────────────────────────────────────────────
async def call_preprocessing(
    image_bytes: bytes,
    filename: str,
    content_type: str,
    product: str,
    text_prompt: str,
) -> bytes:
    """원본 이미지를 preprocessing-service로 보내 cropped 이미지 받기."""
    async with httpx.AsyncClient(timeout=PREP_TIMEOUT) as client:
        files = {"file": (filename, image_bytes, content_type)}
        data = {"product": product, "text_prompt": text_prompt}
        response = await client.post(f"{PREPROCESSING_URL}/crop", files=files, data=data)
        response.raise_for_status()
        cropped_b64 = response.json()["cropped_image"]
        return base64.b64decode(cropped_b64)


async def call_model(model_url: str, cropped_bytes: bytes) -> httpx.Response:
    """Cropped 이미지를 PatchCore 모델 Service로 보내 추론 결과 받기."""
    async with httpx.AsyncClient(timeout=MODEL_TIMEOUT) as client:
        files = {"file": ("cropped.png", cropped_bytes, "image/png")}
        response = await client.post(f"{model_url}/predict", files=files)
        response.raise_for_status()
        return response


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────
@app.post("/predict")
async def infer(
    file: UploadFile = File(...),
    model: str = Form(...),
):  
    if model == "CMH":
        shape = "circle"
    elif model == "EMH":
        shape = "ellipse"
    elif model == "RMH":
        shape = "rectangle"

    if shape not in MODEL_ROUTES:
        raise HTTPException(status_code=400, detail=f"Unknown model: {shape}")

    image_bytes = await file.read()
    text_prompt = PRODUCT_PROMPTS[shape]

    # 1. Preprocessing: GroundedSAM2로 객체 crop
    try:
        cropped_bytes = await call_preprocessing(
            image_bytes, file.filename, file.content_type, shape, text_prompt,
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"Preprocessing returned {e.response.status_code}: {e.response.text}")
        raise HTTPException(
            status_code=502,
            detail=f"Preprocessing failed: HTTP {e.response.status_code}",
        )
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        logger.error(f"Preprocessing unreachable: {type(e).__name__}: {e}")
        raise HTTPException(status_code=503, detail="Preprocessing service unavailable")

    # 2. Inference
    try:
        response = await call_model(MODEL_ROUTES[shape], cropped_bytes)
    except httpx.HTTPStatusError as e:
        logger.error(f"Model {shape} returned {e.response.status_code}: {e.response.text}")
        raise HTTPException(
            status_code=502,
            detail=f"Model inference failed: HTTP {e.response.status_code}",
        )
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        logger.error(f"Model {shape} unreachable: {type(e).__name__}: {e}")
        raise HTTPException(status_code=503, detail=f"Model service unavailable: {shape}")

    return JSONResponse(status_code=response.status_code, content=response.json())


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    return {
        "status": "ready",
        "routes": MODEL_ROUTES,
        "prompts": PRODUCT_PROMPTS,
    }