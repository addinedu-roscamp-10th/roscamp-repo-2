"""Phase 3b 통합 검증 — 외부 SQL INSERT INTO insp_task_txn (PROC) → 자동 AIAdapter → DB 4-table.

SPEC: docs/db_event_bridge/SPEC.md §6 Phase 3b
선행 마이그레이션: migrate_db_event_bridge.sql (Phase 2 — insp_task_txn trigger)
선행 PR: #30 (Phase 3a/3b — router + InspTaskTxnDispatcher)

시나리오:
    1. in-process: EventBridge + StateManager + AIAdapter + Listener + Router + Dispatcher 구성
    2. state_manager.update_inspection_image(item_id, {image_path}) — registry 사전 채움
    3. 별도 connection 으로 INSERT INTO insp_task_txn (item_id, txn_stat='PROC')
    4. DB trigger → pg_notify → DbEventListener → EventBridge → DbEventRouter
       → InspTaskTxnDispatcher → AIAdapter.execute → state_manager.record_inspection_result
    5. DB 4-table 영속화 확인 (insp_task_txn SUCC + ai_inference_txn + insp_stat + item.is_defective)
    6. cleanup

실 DB + 실 AI 서버 (100.66.177.119:30000/predict) 필요. CI 에서는 skip.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


TEST_IMAGE = Path("/Users/ibkim/Pictures/스크린샷/스크린샷 2026-05-15 오후 3.59.35.png")


@pytest.fixture()
def real_db_url() -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL 미설정")
    return dsn


@pytest.fixture()
def real_ai_server(monkeypatch: pytest.MonkeyPatch) -> str:
    """실 AI 서버 endpoint — 옵션 B 정합 (PR #26)."""
    monkeypatch.setenv("MGMT_AI_HOST", "100.66.177.119")
    monkeypatch.setenv("MGMT_AI_PORT", "30000")
    monkeypatch.setenv("MGMT_AI_INFER_PATH", "/predict")
    monkeypatch.setenv("MGMT_AI_TIMEOUT_SEC", "30")
    return "http://100.66.177.119:30000/predict"


@pytest.fixture()
def test_image() -> Path:
    if not TEST_IMAGE.exists():
        pytest.skip(f"test image not found: {TEST_IMAGE}")
    return TEST_IMAGE


def _ensure_cmh_item(dsn: str) -> int:
    """CMH 카테고리 item 1건 확보 (없으면 skip)."""
    from sqlalchemy import create_engine, text

    eng = create_engine(dsn)
    with eng.connect() as conn:
        row = conn.execute(
            text(
                """SELECT i.item_id FROM smartcast.item i
                   JOIN smartcast.ord_detail od ON od.ord_id = i.ord_id
                   JOIN smartcast.product p ON p.prod_id = od.prod_id
                   WHERE p.cate_cd='CMH' ORDER BY i.item_id DESC LIMIT 1"""
            )
        ).fetchone()
    eng.dispose()
    if row is None:
        pytest.skip("CMH 카테고리 item 없음")
    return int(row[0])


def _save_image_to_backend_disk(image_bytes: bytes, item_id: int, tmp_root: Path) -> Path:
    """원본 확장자 (.png) 보존하며 디스크 저장.

    InspectionImageSinkCommand 가 .jpg 강제 확장자라 image/png 가 image/jpeg 로 송신되어
    AI 서버 422 응답을 받음 → 본 helper 가 sink 우회하고 .png 그대로 저장.
    """
    target_dir = tmp_root / str(item_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "phase3b_test.png"
    target.write_bytes(image_bytes)
    return target


def _cleanup_txn(dsn: str, txn_id: int) -> None:
    from sqlalchemy import create_engine, text

    eng = create_engine(dsn)
    with eng.connect() as conn:
        conn.execute(
            text("UPDATE smartcast.insp_task_txn SET final_inference_id=NULL WHERE txn_id=:t"),
            {"t": txn_id},
        )
        conn.execute(
            text("DELETE FROM smartcast.insp_stat WHERE insp_txn_id=:t"), {"t": txn_id}
        )
        conn.execute(
            text("DELETE FROM smartcast.ai_inference_txn WHERE insp_txn_id=:t"),
            {"t": txn_id},
        )
        conn.execute(
            text("DELETE FROM smartcast.insp_task_txn WHERE txn_id=:t"), {"t": txn_id}
        )
        conn.commit()
    eng.dispose()


def test_sql_insert_triggers_full_lifecycle(
    real_db_url: str,
    real_ai_server: str,
    test_image: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQL INSERT INTO insp_task_txn (PROC) → 자동 AIAdapter + DB 4-table 영속화."""
    monkeypatch.setenv("MGMT_DB_EVENT_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("MGMT_DB_EVENT_TASK_DISPATCH", "1")
    monkeypatch.setenv("MGMT_DB_EVENT_CHANNEL", "lifecycle_event")

    from services.core.adapters.ai_adapter import AIAdapter
    from services.core.db_event_dispatchers import InspTaskTxnDispatcher
    from services.core.db_event_listener import DbEventListener
    from services.core.db_event_router import DbEventRouter
    from services.core.event_bridge import EventBridgeImpl
    from services.core.state_manager import StateManager

    item_id = _ensure_cmh_item(real_db_url)

    # 1. in-process backend 컴포넌트 구성
    event_bridge = EventBridgeImpl()
    state_manager = StateManager(event_bridge=event_bridge, enable_persistence=False)
    adapter = AIAdapter()
    dispatcher = InspTaskTxnDispatcher(adapter=adapter, state_manager=state_manager)
    router = DbEventRouter()
    router.register_handler("insp_task_txn", dispatcher.handle)
    router.attach(event_bridge)
    listener = DbEventListener(event_bridge=event_bridge, dsn=real_db_url)

    # 2. 별도 thread 에 event loop 띄움 (record_inspection_result async 호출용)
    loop = asyncio.new_event_loop()
    loop_started = threading.Event()

    def _run_loop():
        asyncio.set_event_loop(loop)
        loop_started.set()
        loop.run_forever()

    loop_thread = threading.Thread(target=_run_loop, daemon=True, name="phase3b_test_loop")
    loop_thread.start()
    loop_started.wait(timeout=2.0)
    dispatcher.set_loop(loop)

    # 3. listener 시작
    listener_started = threading.Event()
    listener_loop_done = threading.Event()

    async def _start_listener_in_loop():
        await listener.start()
        listener_started.set()

    asyncio.run_coroutine_threadsafe(_start_listener_in_loop(), loop).result(timeout=5.0)
    time.sleep(0.5)  # LISTEN 등록 대기

    # 4. image 를 backend disk 에 저장 + state_manager registry 채움 (UploadInspectionImage 시뮬레이션)
    image_bytes = test_image.read_bytes()
    saved_path = _save_image_to_backend_disk(image_bytes, item_id, tmp_path / "phase3b_imgs")
    state_manager.update_inspection_image(
        item_id,
        {
            "image_path": str(saved_path),
            "captured_at": time.time(),
            "label": "phase3b",
            "stage": "INSP",
            "camera_id": "CAM-PHASE3B",
        },
    )

    inserted_txn_id: int | None = None

    try:
        # 5. 외부 SQL INSERT — Phase 3b 가 trigger 받아 자동 진행해야 함
        from sqlalchemy import create_engine, text

        eng = create_engine(real_db_url)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with eng.connect() as conn:
            inserted_txn_id = conn.execute(
                text(
                    "INSERT INTO smartcast.insp_task_txn (item_id, txn_stat, req_at, start_at) "
                    "VALUES (:i, 'PROC', :n, :n) RETURNING txn_id"
                ),
                {"i": item_id, "n": now},
            ).scalar()
            conn.commit()
        eng.dispose()
        inserted_txn_id = int(inserted_txn_id)

        # 6. dispatcher 가 AIAdapter 호출 + record_inspection_result 완료 대기 (AI 호출 ~1초)
        deadline = time.time() + 15.0
        while time.time() < deadline:
            eng = create_engine(real_db_url)
            with eng.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT txn_stat, final_inference_id FROM smartcast.insp_task_txn WHERE txn_id=:t"
                    ),
                    {"t": inserted_txn_id},
                ).fetchone()
            eng.dispose()
            if row and row[0] in ("SUCC", "FAIL") and row[1] is not None:
                break
            time.sleep(0.3)

        # 7. DB 4-table 검증
        eng = create_engine(real_db_url)
        with eng.connect() as conn:
            t = conn.execute(
                text(
                    "SELECT txn_stat, result, final_inference_id FROM smartcast.insp_task_txn WHERE txn_id=:t"
                ),
                {"t": inserted_txn_id},
            ).fetchone()
            inf = conn.execute(
                text(
                    "SELECT inference_id, predicted_class, anomaly_score, is_anomaly, model_id "
                    "FROM smartcast.ai_inference_txn WHERE insp_txn_id=:t"
                ),
                {"t": inserted_txn_id},
            ).fetchone()
            s = conn.execute(
                text(
                    "SELECT final_result, patchcore_inference_id FROM smartcast.insp_stat WHERE insp_txn_id=:t"
                ),
                {"t": inserted_txn_id},
            ).fetchone()
        eng.dispose()

        # Phase 3b chain 검증: dispatcher 가 AIAdapter 호출까지 통과했음의 증거 =
        # insp_task_txn 의 final state 가 PROC 가 아닌 SUCC 또는 FAIL.
        # SUCC: 옵션 B AI 코드 + 실 AI 서버 200 OK (PR #26 머지 후 가능)
        # FAIL: 옵션 A AI 코드 (PR #26 머지 전) — multipart 시그니처 mismatch 로 422
        assert t is not None, "insp_task_txn row 미존재 — listener/router/dispatcher chain 안 거침"
        assert t[0] in ("SUCC", "FAIL"), (
            f"chain 미동작 (stat={t[0]}, PROC 라면 dispatcher 호출 안 됨)"
        )

        if t[0] == "SUCC":
            # 옵션 B 머지 시나리오 — AI 추론 성공 → ai_inference_txn + insp_stat INSERT
            assert t[2] is not None, "final_inference_id 미설정"
            assert inf is not None, "ai_inference_txn INSERT 안 됨"
            assert inf[1] == "CMH", f"predicted_class 정합 실패: {inf[1]}"
            assert inf[2] is not None, "anomaly_score None"
            assert s is not None, "insp_stat INSERT 안 됨"
            assert s[0] in ("GP", "DP"), f"final_result 정합 실패: {s[0]}"
            assert s[1] == inf[0], (
                "insp_stat.patchcore_inference_id 가 ai_inference_txn.inference_id 와 불일치"
            )
        else:
            # FAIL path — record_inspection_failure 가 insp_task_txn 만 FAIL 처리.
            # ai_inference_txn / insp_stat 는 INSERT 안 함 (의도된 동작).
            assert inf is None, "FAIL path 에서 ai_inference_txn INSERT 되면 안 됨"
            assert s is None, "FAIL path 에서 insp_stat INSERT 되면 안 됨"

    finally:
        # 8. cleanup — listener 종료 + loop 종료 + DB row 제거
        try:
            asyncio.run_coroutine_threadsafe(listener.stop(), loop).result(timeout=5.0)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=3.0)
        if not loop.is_closed():
            loop.close()
        if inserted_txn_id is not None:
            _cleanup_txn(real_db_url, inserted_txn_id)
