# ── 베이스 이미지 ───────────────────────────────────────────
# torch 2.11.0+cu128 기준 → CUDA 12.8, cuDNN 9
# GPU: RTX 2080 Ti / Driver: 580.126.20
FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime

# ── 시스템 패키지 ───────────────────────────────────────────
RUN apt-get update -o Acquire::Retries=3 && apt-get install -y \
    libgl1 \
    libglib2.0-0t64 \
    libsm6 \
    libxext6 \
    libxrender1 \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# ── 가상환경 생성 및 활성화 ──────────────────────────────────
RUN pip install --break-system-packages uv

# ── 작업 디렉토리 ───────────────────────────────────────────
WORKDIR /app

# ── 나머지 패키지 설치 ──────────────────────────────────────
COPY requirements.txt .
RUN uv pip install --system --break-system-packages --no-cache-dir -r requirements.txt

# ── 소스 복사 ───────────────────────────────────────────────
COPY server.py .
COPY utils.py .
COPY schemas.py .

# ── 포트 ────────────────────────────────────────────────────
EXPOSE 8080

# ── 실행 ────────────────────────────────────────────────────
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
