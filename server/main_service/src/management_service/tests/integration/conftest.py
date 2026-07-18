from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


THIS_DIR = Path(__file__).resolve().parent
TESTS_DIR = THIS_DIR.parent
MANAGEMENT_ROOT = TESTS_DIR.parent.parent
MAIN_SERVICE_SRC = MANAGEMENT_ROOT.parent
MAIN_SERVICE_ROOT = MAIN_SERVICE_SRC.parent.parent
SERVER_ROOT = MAIN_SERVICE_ROOT.parent

for path in (
    str(MANAGEMENT_ROOT),
    str(MAIN_SERVICE_SRC),
    str(MAIN_SERVICE_ROOT),
    str(SERVER_ROOT),
):
    if path not in sys.path:
        sys.path.insert(0, path)


_DUMMY_DATABASE_URL = "postgresql+psycopg2://test:test@127.0.0.1:65500/test"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _database_url_or_skip() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url or url == _DUMMY_DATABASE_URL:
        pytest.skip("integration DB tests require a real DATABASE_URL")
    return url


def _quote_ident(name: str) -> str:
    if not _IDENT_RE.fullmatch(name):
        raise RuntimeError(f"invalid schema identifier: {name!r}")
    return f'"{name}"'


@dataclass
class RuntimeRepoHarness:
    schema: str
    sync_engine: object
    async_engine: object
    sync_session_factory: sessionmaker
    async_session_factory: sessionmaker
    repo: object
    models: object
    base_metadata: object


@pytest.fixture(scope="session")
def runtime_repo_harness() -> Iterator[RuntimeRepoHarness]:
    database_url = _database_url_or_skip()
    schema = os.environ.get("SMARTCAST_TEST_SCHEMA", "smartcast_test").strip() or "smartcast_test"
    schema_map = {"smartcast": schema}

    import smart_cast_db.models as models
    from smart_cast_db.database import Base
    from services.persistence.runtime_state_repository import RuntimeStateRepository

    base_sync_engine = create_engine(
        database_url,
        connect_args={"options": "-c timezone=Asia/Seoul"},
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
    )
    translated_sync_engine = base_sync_engine.execution_options(schema_translate_map=schema_map)

    base_async_engine = create_async_engine(
        database_url,
        connect_args={"options": "-c timezone=Asia/Seoul"},
        echo=False,
    )
    translated_async_engine = base_async_engine.execution_options(schema_translate_map=schema_map)

    with base_sync_engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_quote_ident(schema)} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {_quote_ident(schema)}"))
    Base.metadata.create_all(bind=translated_sync_engine)

    seed_sql_path = MAIN_SERVICE_SRC.parent / "smart_cast_db" / "seed" / "seed_master.sql"
    if seed_sql_path.exists():
        import psycopg
        psql_dsn = database_url.replace("postgresql+psycopg://", "postgresql://")
        try:
            with psycopg.connect(psql_dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {_quote_ident(schema)};")
                    cur.execute(seed_sql_path.read_text(encoding="utf-8"))
                    print(f"\n>>> HARNESS: Successfully seeded master data to {schema}!")
        except Exception as e:
            print(f"\n>>> HARNESS ERROR SEEDING: {e}", flush=True)
            import traceback
            traceback.print_exc()
            raise e

    sync_session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=translated_sync_engine,
    )
    async_session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=translated_async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    repo = RuntimeStateRepository(
        sync_session_factory,
        async_session_factory,
        ord_model=models.Ord,
        ord_detail_model=models.OrdDetail,
        ord_stat_model=models.OrdStat,
        ord_log_model=models.OrdLog,
        pattern_model=models.Pattern,
        item_model=models.ItemStat,
        equip_task_txn_model=models.EquipTaskTxn,
        insp_task_txn_model=models.InspTaskTxn,
        trans_task_txn_model=models.TransTaskTxn,
        pp_task_txn_model=models.PpTaskTxn,
        equip_stat_model=models.EquipStat,
        trans_stat_model=models.TransStat,
        chg_location_stat_model=models.ChgLocationStat,
        strg_location_stat_model=models.StrgLocationStat,
        log_event_model=models.LogEvent,
        log_err_equip_model=models.LogErrEquip,
        log_err_trans_model=models.LogErrTrans,
        tat_nav_pose_master_model=models.TatNavPoseMaster,
        trans_task_bat_threshold_model=models.TransTaskBatThreshold,
    )

    yield RuntimeRepoHarness(
        schema=schema,
        sync_engine=translated_sync_engine,
        async_engine=translated_async_engine,
        sync_session_factory=sync_session_factory,
        async_session_factory=async_session_factory,
        repo=repo,
        models=models,
        base_metadata=Base.metadata,
    )

    with base_sync_engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_quote_ident(schema)} CASCADE"))
    base_sync_engine.dispose()
    import asyncio
    asyncio.run(base_async_engine.dispose())


@pytest.fixture()
def runtime_repo_db(runtime_repo_harness: RuntimeRepoHarness) -> RuntimeRepoHarness:
    schema = runtime_repo_harness.schema
    sync_engine = runtime_repo_harness.sync_engine

    with sync_engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_quote_ident(schema)} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {_quote_ident(schema)}"))
    runtime_repo_harness.base_metadata.create_all(bind=sync_engine)

    seed_sql_path = MAIN_SERVICE_SRC.parent / "smart_cast_db" / "seed" / "seed_master.sql"
    if seed_sql_path.exists():
        import psycopg
        database_url = _database_url_or_skip()
        psql_dsn = database_url.replace("postgresql+psycopg://", "postgresql://")
        try:
            with psycopg.connect(psql_dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {_quote_ident(schema)};")
                    cur.execute(seed_sql_path.read_text(encoding="utf-8"))
                    print(f"\n>>> REPO_DB: Successfully seeded master data to {schema}!")
        except Exception as e:
            print(f"\n>>> REPO_DB ERROR SEEDING: {e}", flush=True)
            import traceback
            traceback.print_exc()
            raise e

    return runtime_repo_harness
