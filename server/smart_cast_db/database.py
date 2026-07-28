"""Database engine and session factory.

Shared DB entrypoint for web/app and management services.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(BASE_DIR)


def _load_env_local() -> None:
    """Load `.env.local` from known service roots without overriding existing env."""
    candidates = [
        Path(SERVER_DIR) / "main_service" / ".env.local",
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return


_load_env_local()

DATABASE_URL: str | None = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL 환경변수가 설정되지 않았습니다. "
        "main_service/.env.local 또는 실행 환경에 PostgreSQL 연결 문자열을 설정하세요. "
        "예: DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname"
    )
if DATABASE_URL.startswith("sqlite"):
    raise RuntimeError(
        "SQLite 는 더 이상 지원되지 않습니다 (2026-04-14). "
        "PostgreSQL 연결 문자열을 사용하세요: postgresql+psycopg://..."
    )


def _build_engine(url: str) -> Engine:
    return create_engine(
        url,
        # 모든 timestamp default(now()) 를 KST 기준으로 저장/조회하기 위해
        # 세션 타임존을 Asia/Seoul 로 고정한다.
        connect_args={"options": "-c timezone=Asia/Seoul"},
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
    )


engine: Engine = _build_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# 비동기 엔진 및 세션 팩토리 추가 정의 (gRPC 및 Orchestrator 비동기화 지원)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

async_db_url = DATABASE_URL
if DATABASE_URL and "://" in DATABASE_URL:
    _, remain = DATABASE_URL.split("://", 1)
    async_db_url = f"postgresql+psycopg://{remain}"

async_engine = create_async_engine(
    async_db_url,
    connect_args={"options": "-c timezone=Asia/Seoul"},
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=False,
)
AsyncSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
