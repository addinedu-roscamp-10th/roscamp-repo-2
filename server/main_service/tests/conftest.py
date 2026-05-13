"""main_service pytest 공통 설정 — DB 미연결 환경에서도 import 가능하도록."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MANAGEMENT_SRC = SRC / "management_service"
SHARED_DB = ROOT.parent / "smart_cast_db"

for path in (str(MANAGEMENT_SRC), str(SRC), str(SHARED_DB.parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

# DB 모듈은 import-time 에 DATABASE_URL 을 요구하므로 더미 값을 미리 주입.
# 실 connect 은 SessionLocal() 호출 시점에 시도되므로 import 단계에서는 안전.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://test:test@127.0.0.1:65500/test",
)


@pytest.fixture()
def jpeg_bytes() -> bytes:
    """최소 JPEG SOI/EOI 헤더 + dummy payload (실제 디코딩은 안 함)."""
    return b"\xff\xd8\xff\xe0" + b"mock-jpeg-payload" * 8 + b"\xff\xd9"
