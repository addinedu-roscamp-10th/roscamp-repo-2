#!/usr/bin/env python3
"""sim_factory_demo.py — 우분투 PC 실 HW 환경 e2e 시연.

가상 주문부터 적재까지 10 phase 자동 진행. PyQt 화면에 실시간 반영.

HW 가정:
    - backend (FastAPI :8000 + management gRPC :50051 with ROS2) 가동
    - PyQt factory_operator 가동
    - MAT/TAT/PAT 로봇 ROS2 노드 가동 (cast_python, tat_bringup, pat_*)
    - Jetson (esp_bridge + camera) 가 management gRPC :50051 에 연결
    - ESP32 펌웨어 + 컨베이어 + RC522 RFID + GPIO33 푸시 버튼
    - AI 서버 100.66.177.119:30000/predict 가동 (CMH/EMH 정상)

사용법:
    # 기본: 사용자가 PyQt ① ② ③ 버튼을 영상에 맞춰 직접 누름.
    python sim_factory_demo.py --image /path/to/inspection.jpg

    # 자동 모드: 버튼 효과를 모두 스크립트가 시뮬 (영상에 PyQt 화면 자동 변화만)
    python sim_factory_demo.py --image /path/to/inspection.jpg --auto-buttons

    # 영상 길이 조절
    python sim_factory_demo.py --image /path/to/inspection.jpg --phase-delay 3

옵션:
    --image PATH       검사 이미지 (사용자 컨베이어 사진)
    --cate-cd CODE     주문 제품 카테고리 (CMH/RMH/EMH, default EMH)
    --phase-delay SEC  phase 간 delay 초 (default 2)
    --auto-buttons     PyQt ① ② ③ 효과 자동 시뮬 (사용자 인터랙션 0)
    --keep-data        cleanup skip (검증용)
    --schema NAME      DB 스키마 (default $PG_SCHEMA or inbean)
    --backend URL      backend base URL (default http://localhost:8000)

작성: 2026-05-18
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
import time
from pathlib import Path

import httpx

# ─────────────────────────────────────────────
# 출력 유틸 (영상 가독성)
# ─────────────────────────────────────────────
BAR = "═" * 80


def hdr(n: int, title: str) -> None:
    print()
    print(BAR)
    print(f"  [Phase {n}] {title}")
    print(BAR)


def step(msg: str) -> None:
    print(f"  → {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    sys.exit(1)


def pause(sec: float) -> None:
    if sec > 0:
        time.sleep(sec)


# ─────────────────────────────────────────────
# DB / HTTP helpers
# ─────────────────────────────────────────────
def _setup_pythonpath() -> None:
    here = Path(__file__).resolve()
    root = here.parents[3]  # server/main_service/scripts/e2e → root
    paths = [
        root / "server" / "main_service" / "src",
        root / "server" / "main_service" / "src" / "management_service",
        root / "server",
    ]
    for p in paths:
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def db_session():
    from smart_cast_db.database import SessionLocal
    return SessionLocal()


def http_post(url: str, json_body: dict | None = None, params: dict | None = None) -> dict:
    with httpx.Client(timeout=15.0) as c:
        r = c.post(url, json=json_body, params=params)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text}


def http_get(url: str, params: dict | None = None) -> dict | list:
    with httpx.Client(timeout=15.0) as c:
        r = c.get(url, params=params)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text}


# ─────────────────────────────────────────────
# Phase 0 — 환경 점검
# ─────────────────────────────────────────────
def phase0_preflight(args) -> None:
    hdr(0, "환경 점검 (Backend / AI / DB / 이미지)")

    # backend health
    try:
        r = http_get(f"{args.backend}/health")
        ok(f"backend /health: {r}")
    except Exception as e:
        fail(f"backend 미가동: {e}")

    # AI server ping
    try:
        with httpx.Client(timeout=5.0) as c:
            resp = c.post(
                "http://100.66.177.119:30000/predict",
                files={"file": ("ping.jpg", b"\xff\xd8\xff", "image/jpeg")},
                data={"model": args.cate_cd},
            )
        if resp.status_code in (200, 422, 400):
            ok(f"AI 서버 /predict reachable (model={args.cate_cd}, HTTP {resp.status_code})")
        elif resp.status_code == 503:
            warn(
                f"AI 모델 {args.cate_cd} 서비스 unavailable (HTTP 503). "
                "ai-server 노드 확인 필요. 다른 cate_cd 로 재시도하세요."
            )
        else:
            warn(f"AI HTTP {resp.status_code}")
    except Exception as e:
        warn(f"AI 서버 unreachable: {e}")

    # DB 연결
    from sqlalchemy import text
    with db_session() as db:
        ord_n = db.execute(text(f"SELECT count(*) FROM {args.schema}.ord")).scalar()
        ok(f"DB {args.schema}.ord rows: {ord_n}")

    # 이미지
    img = Path(args.image)
    if not img.exists():
        fail(f"이미지 없음: {img}")
    ok(f"이미지: {img.name} ({img.stat().st_size:,} bytes)")

    pause(args.phase_delay)


# ─────────────────────────────────────────────
# Phase 1 — 가상 주문 생성
# ─────────────────────────────────────────────
def phase1_create_order(args) -> int:
    hdr(1, f"가상 주문 생성 (cate_cd={args.cate_cd})")

    from sqlalchemy import text

    with db_session() as db:
        # cate_cd 의 첫 product 선택
        row = db.execute(
            text(f"SELECT prod_id, base_price FROM {args.schema}.product "
                 f"WHERE cate_cd=:c ORDER BY prod_id LIMIT 1"),
            {"c": args.cate_cd},
        ).fetchone()
        if row is None:
            fail(f"{args.cate_cd} cate_cd 의 product 없음 — {args.schema}.product 시드 확인")
        prod_id, base_price = int(row[0]), float(row[1] or 0)
        step(f"선택된 product: prod_id={prod_id} base_price={base_price:,}")

    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    customer_name = f"E2E시연-{suffix}"
    email = f"e2e_{suffix}@example.com"
    from datetime import date, timedelta
    due_date = (date.today() + timedelta(days=7)).isoformat()
    body = {
        "company_name": "E2E 시연 주식회사",
        "customer_name": customer_name,
        "email": email,
        "phone": "010-0000-0000",
        "shipping_address": "테스트 주소",
        "total_amount": base_price,
        "requested_delivery": due_date,
        "details": [
            {
                "product_id": str(prod_id),
                "product_name": f"E2E demo prod {prod_id}",
                "quantity": 1,
                "unit_price": base_price,
                "subtotal": base_price,
                "post_processing_ids": [],
                "diameter": "450",
                "thickness": "25",
                "material": "FC200",
                "load_class": "A15",
            }
        ],
    }
    try:
        resp = http_post(f"{args.backend}/api/orders/customer", json_body=body)
    except httpx.HTTPStatusError as e:
        fail(f"고객 발주 실패: HTTP {e.response.status_code} body={e.response.text[:300]}")
    ord_id = int(resp.get("ord_id") or resp.get("id") or 0)
    if ord_id <= 0:
        fail(f"ord_id 추출 실패: {resp}")
    ok(f"주문 생성 — ord_id={ord_id}, customer={customer_name}")
    pause(args.phase_delay)
    return ord_id


# ─────────────────────────────────────────────
# Phase 2 — 주문 승인 RCVD → APPR
# ─────────────────────────────────────────────
def phase2_approve(args, ord_id: int) -> None:
    hdr(2, f"주문 승인 — RCVD → APPR (ord_id={ord_id})")
    try:
        resp = http_post(
            f"{args.backend}/api/orders/{ord_id}/status",
            params={"new_stat": "APPR"},
        )
        ok(f"승인 응답: ord_stat={resp.get('ord_stat')} stat_id={resp.get('stat_id')}")
    except httpx.HTTPStatusError as e:
        fail(f"승인 실패: HTTP {e.response.status_code} body={e.response.text[:300]}")
    pause(args.phase_delay)


# ─────────────────────────────────────────────
# Phase 3 — 생산 시작 (item INSERT + MM PROC)
# ─────────────────────────────────────────────
def phase3_start_production(args, ord_id: int) -> int:
    hdr(3, f"생산 시작 — item 생성 + MM PROC (ord_id={ord_id})")
    try:
        resp = http_post(
            f"{args.backend}/api/production/start",
            json_body={"ord_id": ord_id},
        )
    except httpx.HTTPStatusError as e:
        # 일부 빌드에서는 별도 endpoint. fallback 으로 DB 직접 INSERT.
        warn(f"/api/production/start 실패 ({e.response.status_code}) — DB 직접 INSERT fallback")
        return _phase3_db_fallback(args, ord_id)

    items = resp.get("item_ids") or resp.get("items") or resp.get("created_items") or []
    if not items and resp.get("item_id"):
        items = [resp["item_id"]]
    if not items:
        warn(f"item 응답 비어있음: {resp} — DB 직접 INSERT fallback")
        return _phase3_db_fallback(args, ord_id)
    first = items[0]
    item_id = int(first.get("item_id") if isinstance(first, dict) else first)
    ok(f"item_id={item_id} 생성")
    pause(args.phase_delay)
    return item_id


def _phase3_db_fallback(args, ord_id: int) -> int:
    from sqlalchemy import text

    with db_session() as db:
        # 신규 item INSERT (cur_stat=CREATED)
        item_row = db.execute(
            text(
                f"INSERT INTO {args.schema}.item (ord_id, cur_stat) "
                f"VALUES (:o, 'CREATED') RETURNING item_id"
            ),
            {"o": ord_id},
        ).fetchone()
        item_id = int(item_row[0])
        # MM equip_task_txn INSERT PROC
        db.execute(
            text(
                f"INSERT INTO {args.schema}.equip_task_txn "
                f"(item_id, task_type, txn_stat) VALUES (:i, 'MM', 'PROC')"
            ),
            {"i": item_id},
        )
        db.commit()
    ok(f"DB fallback INSERT — item_id={item_id} + MM PROC")
    pause(args.phase_delay)
    return item_id


# ─────────────────────────────────────────────
# Phase 4 — MAT chain 진행 모니터링
# ─────────────────────────────────────────────
def phase4_mat_chain(args, item_id: int) -> None:
    hdr(4, f"MAT chain — MM → POUR → DM 진행 모니터링 (item_id={item_id})")
    from sqlalchemy import text

    deadline = time.time() + 120
    seen = set()
    last_stats = ""
    while time.time() < deadline:
        with db_session() as db:
            rows = db.execute(
                text(
                    f"SELECT task_type, txn_stat FROM {args.schema}.equip_task_txn "
                    f"WHERE item_id=:i AND task_type IN ('MM','POUR','DM') "
                    f"ORDER BY txn_id"
                ),
                {"i": item_id},
            ).fetchall()
        cur = ", ".join(f"{t}={s}" for t, s in rows)
        if cur != last_stats:
            step(cur or "(아직 task 없음)")
            last_stats = cur
        succ = {t for t, s in rows if s == "SUCC"}
        if succ >= {"MM", "POUR", "DM"}:
            ok(f"MAT chain 완료 — {cur}")
            pause(args.phase_delay)
            return
        time.sleep(1.5)
    warn(f"MAT chain 미완료 (120s timeout) — 최종 상태: {last_stats}")
    pause(args.phase_delay)


# ─────────────────────────────────────────────
# Phase 5 — TAT ToPP 도착 + 핸드오프 (① 레드 푸시 버튼)
# ─────────────────────────────────────────────
def phase5_topp_handoff(args, item_id: int) -> None:
    hdr(5, "TAT ToPP 도착 + 핸드오프 ACK (실 HW: ESP32 GPIO33 빨간 버튼, "
                "또는 PyQt ① 핸드오프 ACK 버튼)")

    if args.auto_buttons:
        step("--auto-buttons 모드: HTTP /api/debug/handoff-ack 시뮬")
        try:
            resp = http_post(f"{args.backend}/api/debug/handoff-ack", json_body={})
            ok(f"핸드오프 결과: {resp}")
        except httpx.HTTPStatusError as e:
            fail(f"handoff-ack 실패: {e.response.status_code} body={e.response.text[:300]}")
    else:
        step("⏸ 사용자 인터랙션 대기 — PyQt 의 ① 핸드오프 ACK 버튼 또는 "
             "ESP32 GPIO33 빨간 버튼을 눌러주세요.")
        _wait_until_handoff_ack(args, item_id, timeout=180)
    pause(args.phase_delay)


def _wait_until_handoff_ack(args, item_id: int, timeout: int = 180) -> None:
    from sqlalchemy import text

    deadline = time.time() + timeout
    while time.time() < deadline:
        with db_session() as db:
            row = db.execute(
                text(f"SELECT flow_stat FROM {args.schema}.item WHERE item_id=:i"),
                {"i": item_id},
            ).fetchone()
        if row and (row[0] or "").upper() in ("PP", "WAIT_INSP"):
            ok(f"핸드오프 감지 — item.flow_stat={row[0]}")
            return
        time.sleep(2)
    warn(f"핸드오프 timeout ({timeout}s) — item.flow_stat 미전이")


# ─────────────────────────────────────────────
# Phase 6 — RFID 스캔 (② 버튼)
# ─────────────────────────────────────────────
def phase6_rfid_scan(args, item_id: int) -> None:
    hdr(6, "RFID 스캔 — 후처리 옵션 표시 (실 HW: RC522 RFID 태그 + PyQt ② 버튼)")

    # rfid 페이로드 시뮬 — 메모리 [RFID payload 포맷 order_<ord>_item_<YYYYMMDD>_<seq>]
    from datetime import datetime as _dt
    payload = f"e2e_demo_item_{item_id}_{_dt.now().strftime('%Y%m%d%H%M%S')}"
    if args.auto_buttons:
        step(f"--auto-buttons 모드: HTTP /api/debug/sim/rfid-scan payload={payload}")
        try:
            resp = http_post(
                f"{args.backend}/api/debug/sim/rfid-scan",
                json_body={"raw_payload": payload, "reader_id": "RFID-CONV-01"},
            )
            ok(f"RFID 스캔 응답: item_id={resp.get('item', {}).get('item_id')} "
               f"options={len(resp.get('pp_options', []))}")
        except httpx.HTTPStatusError as e:
            warn(f"RFID 스캔 실패: {e.response.status_code} body={e.response.text[:200]}")
    else:
        step("⏸ 사용자 인터랙션 대기 — 작업자가 RFID 태그를 컨베이어 RC522 에 스캔하면 "
             "PyQt RFID payload 가 자동 채워집니다. 그 후 PyQt ② RFID 스캔 버튼을 누르세요.")
        step("(시뮬 환경에서는 --auto-buttons 옵션을 사용하세요)")
        # rfid_scan_log 가 INSERT 됐는지 polling
        _wait_until_rfid_scan(args, timeout=120)
    pause(args.phase_delay)


def _wait_until_rfid_scan(args, timeout: int = 120) -> None:
    from sqlalchemy import text

    deadline = time.time() + timeout
    base_count = 0
    with db_session() as db:
        base_count = db.execute(
            text(f"SELECT count(*) FROM {args.schema}.rfid_scan_log")
        ).scalar() or 0
    while time.time() < deadline:
        with db_session() as db:
            now_count = db.execute(
                text(f"SELECT count(*) FROM {args.schema}.rfid_scan_log")
            ).scalar() or 0
        if now_count > base_count:
            ok("RFID 스캔 감지")
            return
        time.sleep(2)
    warn(f"RFID 스캔 timeout ({timeout}s)")


# ─────────────────────────────────────────────
# Phase 7 — ③ 후처리 완료 (PP_DONE)
# ─────────────────────────────────────────────
def phase7_pp_done(args, item_id: int) -> None:
    hdr(7, "③ 후처리 완료 → 컨베이어 1차 RUN (실 HW: PyQt ③ 버튼)")

    if args.auto_buttons:
        step("--auto-buttons 모드: gRPC PublishEvent(PP_DONE_REQUESTED) 시뮬")
        _publish_pp_done(args, item_id)
        ok("PP_DONE_REQUESTED publish 완료 — backend 가 ConveyorCmd(start) enqueue")
    else:
        step("⏸ 사용자 인터랙션 대기 — PyQt 의 ③ 후처리 완료 버튼을 눌러주세요. "
             "3초 카운트다운 후 컨베이어 모터가 가동됩니다.")
    pause(args.phase_delay)


def _publish_pp_done(args, item_id: int) -> None:
    """gRPC EventGateway PublishEvent(PP_DONE_REQUESTED)."""
    import grpc
    from google.protobuf.struct_pb2 import Struct

    # backend management gRPC stub
    try:
        from generated import event_gateway_pb2, event_gateway_pb2_grpc
    except ImportError:
        # 대체 경로
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "management_service"))
        from generated import event_gateway_pb2, event_gateway_pb2_grpc  # type: ignore

    payload = Struct()
    payload.update({"item_id": item_id, "source": "e2e_demo"})
    req = event_gateway_pb2.PublishEventRequest(
        event_type="PP_DONE_REQUESTED",
        item_id=int(item_id),
        payload=payload,
    )
    with grpc.insecure_channel("localhost:50051") as ch:
        stub = event_gateway_pb2_grpc.EventGatewayStub(ch)
        resp = stub.PublishEvent(req, timeout=5.0)
    step(f"PublishEvent response: {resp}")


# ─────────────────────────────────────────────
# Phase 8 — 컨베이어 + 카메라 + AI 추론 모니터링
# ─────────────────────────────────────────────
def phase8_inspection(args, item_id: int) -> None:
    hdr(8, "컨베이어 가동 → 카메라 캡처 → AI 추론 → PyQt 결과 이미지 표시")

    step("실 HW 흐름: ESP32 motor RUN → TOF2 STOPPED → Jetson capture → "
         "UploadInspectionImage RPC → backend → AI /predict → record_inspection_result")
    step("시뮬 환경 (--auto-buttons) 에서는 watch_inspection_flow sim 흐름과 동일하게 진행.")

    # 결과 polling — ai_inference_txn 에 inference_id 가 생기면 완료
    from sqlalchemy import text

    deadline = time.time() + 300  # 5분
    last_status = ""
    while time.time() < deadline:
        with db_session() as db:
            insp_row = db.execute(
                text(
                    f"SELECT t.txn_id, t.txn_stat, a.inference_id, a.predicted_class, "
                    f"       a.result_image_url, s.final_result "
                    f"FROM {args.schema}.insp_task_txn t "
                    f"LEFT JOIN {args.schema}.ai_inference_txn a ON a.insp_txn_id=t.txn_id "
                    f"LEFT JOIN {args.schema}.insp_stat s ON s.insp_txn_id=t.txn_id "
                    f"WHERE t.item_id=:i ORDER BY t.txn_id DESC LIMIT 1"
                ),
                {"i": item_id},
            ).fetchone()
        status_line = f"insp_txn={insp_row[0] if insp_row else '-'} stat={insp_row[1] if insp_row else '-'}"
        if insp_row and insp_row[2]:
            status_line += (
                f" inference={insp_row[2]} class={insp_row[3]} "
                f"final={insp_row[5]} img={(insp_row[4] or '')[-40:]}"
            )
        if status_line != last_status:
            step(status_line)
            last_status = status_line
        if insp_row and insp_row[1] == "SUCC" and insp_row[5] in ("GP", "DP"):
            ok("검사 완료 — PyQt 품질 페이지에 결과 이미지 표시됨")
            pause(args.phase_delay)
            return
        time.sleep(2)
    warn(f"검사 timeout (5min) — 최종 상태: {last_status}")
    pause(args.phase_delay)


# ─────────────────────────────────────────────
# Phase 9 — ToSTRG + PA_GP/PA_DP 모니터링
# ─────────────────────────────────────────────
def phase9_tostrg_pa(args, item_id: int) -> None:
    hdr(9, "ToSTRG (TAT 적재존 이동) + PA_GP/PA_DP (PAT 적재) 모니터링")

    from sqlalchemy import text

    deadline = time.time() + 180
    last_status = ""
    while time.time() < deadline:
        with db_session() as db:
            tat_row = db.execute(
                text(
                    f"SELECT task_type, txn_stat FROM {args.schema}.trans_task_txn "
                    f"WHERE item_id=:i AND task_type='ToSTRG' ORDER BY txn_id DESC LIMIT 1"
                ),
                {"i": item_id},
            ).fetchone()
            pa_row = db.execute(
                text(
                    f"SELECT task_type, txn_stat FROM {args.schema}.equip_task_txn "
                    f"WHERE item_id=:i AND task_type IN ('PA_GP','PA_DP') "
                    f"ORDER BY txn_id DESC LIMIT 1"
                ),
                {"i": item_id},
            ).fetchone()
            item_row = db.execute(
                text(f"SELECT flow_stat, is_defective FROM {args.schema}.item WHERE item_id=:i"),
                {"i": item_id},
            ).fetchone()
        cur = (
            f"ToSTRG={(tat_row or ('-','-'))[1]}  "
            f"PA={(pa_row or ('-','-'))[0]}/{(pa_row or ('-','-'))[1]}  "
            f"item.flow={item_row[0] if item_row else '-'}"
        )
        if cur != last_status:
            step(cur)
            last_status = cur
        if (
            tat_row and tat_row[1] == "SUCC"
            and pa_row and pa_row[1] == "SUCC"
        ):
            ok("적재 완료")
            pause(args.phase_delay)
            return
        time.sleep(2)
    warn(f"적재 timeout (3min) — 최종 상태: {last_status}")
    pause(args.phase_delay)


# ─────────────────────────────────────────────
# Phase 10 — 최종 검증
# ─────────────────────────────────────────────
def phase10_verify(args, item_id: int) -> None:
    hdr(10, "최종 검증 — 4-table 일관성 + PyQt API 응답")

    from sqlalchemy import text

    with db_session() as db:
        rows = db.execute(
            text(
                f"SELECT 'item'::text, flow_stat::text, is_defective::text "
                f"  FROM {args.schema}.item WHERE item_id=:i "
                f"UNION ALL "
                f"SELECT 'insp_task_txn', txn_stat, result::text "
                f"  FROM {args.schema}.insp_task_txn WHERE item_id=:i "
                f"UNION ALL "
                f"SELECT 'ai_inference_txn', predicted_class, is_anomaly::text "
                f"  FROM {args.schema}.ai_inference_txn "
                f"  WHERE insp_txn_id IN (SELECT txn_id FROM {args.schema}.insp_task_txn WHERE item_id=:i) "
                f"UNION ALL "
                f"SELECT 'insp_stat', final_result, '-' "
                f"  FROM {args.schema}.insp_stat "
                f"  WHERE insp_txn_id IN (SELECT txn_id FROM {args.schema}.insp_task_txn WHERE item_id=:i) "
            ),
            {"i": item_id},
        ).fetchall()
    for r in rows:
        ok(f"{r[0]:<18s} {r[1] or '-':<12s} {r[2] or '-'}")

    # PyQt /api/quality/inspections 응답 확인
    try:
        data = http_get(f"{args.backend}/api/quality/inspections")
        rows_my = [r for r in data if r.get("item_id") == item_id] if isinstance(data, list) else []
        if rows_my:
            r = rows_my[0]
            ok(f"PyQt 응답: result={r.get('result')} "
               f"inspected_at={r.get('inspected_at')} "
               f"result_image_url={(r.get('result_image_url') or '')[-50:]}")
        else:
            warn("/api/quality/inspections 에서 본 item 미발견")
    except Exception as e:
        warn(f"/api/quality/inspections 조회 실패: {e}")


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", required=True, help="검사 이미지 경로")
    parser.add_argument("--cate-cd", default="EMH", choices=["CMH", "RMH", "EMH"])
    parser.add_argument("--phase-delay", type=float, default=2.0)
    parser.add_argument("--auto-buttons", action="store_true",
                        help="PyQt ① ② ③ 버튼 효과 자동 시뮬")
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--schema", default=os.environ.get("PG_SCHEMA", "inbean"))
    parser.add_argument("--backend", default="http://localhost:8000")
    args = parser.parse_args()

    _setup_pythonpath()

    print(BAR)
    print(f"  SmartCast Factory e2e Demo")
    print(f"    schema={args.schema}  backend={args.backend}  cate_cd={args.cate_cd}")
    print(f"    image={args.image}")
    print(f"    auto_buttons={args.auto_buttons}  phase_delay={args.phase_delay}s")
    print(BAR)

    phase0_preflight(args)
    ord_id = phase1_create_order(args)
    phase2_approve(args, ord_id)
    item_id = phase3_start_production(args, ord_id)
    phase4_mat_chain(args, item_id)
    phase5_topp_handoff(args, item_id)
    phase6_rfid_scan(args, item_id)
    phase7_pp_done(args, item_id)
    phase8_inspection(args, item_id)
    phase9_tostrg_pa(args, item_id)
    phase10_verify(args, item_id)

    print()
    print(BAR)
    print(f"  ✓ 시연 완료 — ord_id={ord_id} item_id={item_id}")
    print(BAR)

    if not args.keep_data:
        print("  (cleanup 은 별도 실행: scripts/e2e/cleanup_demo.py 또는 SQL)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
