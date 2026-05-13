"""HW 풀 e2e 워처 — RA → TAT → CONV1 → (불량 시) RA 재진입 전체 사슬 검증.

각 phase 마다 다층 검증:
  - DB        : 실제 row 전이 폴링
  - Backend   : PyQt/Web 가 의존하는 REST API 가 일치된 데이터 반환하는지 확인
  - HW probe  : (해당 phase 에서) Jetson EventGateway / ESP32 펌웨어 / CONV1 모터
  - 운영자    : PyQt 의 ① 하차완료 / ② RFID 검색 / ③ 후처리 완료 버튼 안내

흐름 (PR #15+#16 이후 자율 흐름 기준):
  Phase 0  : 베이스라인 + Backend / Web health prob
  Phase 1  : 발주 생성 (POST /api/orders/customer) + Web /api/orders 확인
  Phase 2  : APPR 전이
  Phase 3  : 생산 시작 → item + RA1/MM equip_task_txn(QUE) 생성
  Phase 4  : RA 자율 — MM → POUR → DM 각 SUCC 폴링 (사용자 advance 없음)
  Phase 5  : DM SUCC 후 ToPP trans_task_txn 자동 생성 → TAT AMR 운반
             도착 후 ① 하차완료 버튼 (또는 ESP32 GPIO33) → handoff_acks INSERT
             + pp_task_txn (QUE) 생성
  Phase 6  : ② RFID 스캔 (RC522 또는 PyQt 버튼) → log_action_operator_rfid_scan INSERT
  Phase 7  : ③ 후처리 완료 버튼 → 3 초 카운트다운 → POST /api/management/conveyor/CONV-01/start
             → ESP32 모터 ON → pp_task_txn 전체 SUCC + ToINSP equip_task_txn(PROC) 생성
  Phase 8  : 컨베이어 진행 → TOF2 자동 캡처 → UploadInspectionImage RPC →
             insp_task_txn(PROC→SUCC) + ai_inference_txn(YOLO/PatchCore) + insp_stat
  Phase 9  : 결과 분기
             - final_result='GP' → ToPAWait → ToSTRG → PA_GP → item DONE
             - final_result='DP' → tostrg_dld 이벤트 → 대체 item 생성 (RA 재진입!) + PA_DP
  Phase 10 : 4-table 일관성 검증 + cleanup (옵션)

기본 cleanup 활성 (--keep-data 로 보존 가능).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _ensure_path() -> None:
    here = Path(__file__).resolve()
    main_service_root = here.parents[2]
    src = main_service_root / "src"
    for p in [str(src / "management_service"), str(src), str(main_service_root.parent)]:
        if p not in sys.path:
            sys.path.insert(0, p)


_ensure_path()

if not os.environ.get("DATABASE_URL"):
    print("✗ DATABASE_URL 미설정 — scripts/run-e2e-full.sh 로 실행하거나 .env.local 을 export 하세요.")
    sys.exit(2)

# 스키마: .env.local 의 PG_SCHEMA 우선, 없으면 smartcast
os.environ.setdefault("SMARTCAST_SCHEMA", os.environ.get("PG_SCHEMA", "smartcast"))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from smart_cast_db.database import SessionLocal  # noqa: E402

BASE_URL = os.environ.get("E2E_BACKEND_URL", "http://127.0.0.1:8000")
WEB_URL = os.environ.get("E2E_WEB_URL", "http://127.0.0.1:3001")
DEFAULT_PRODUCT = "R-D450"

# 핵심 테이블 목록 (smartcast 스키마 기준, 사전 점검 + cleanup 순서에 사용)
CORE_TABLES = [
    "ord", "ord_pattern", "ord_pp_map", "ord_stat",
    "item", "equip_task_txn", "trans_task_txn",
    "pp_task_txn",
    "log_action_operator_rfid_scan",
    "log_action_operator_handoff_acks",
    "insp_task_txn", "ai_inference_txn", "insp_stat",
]

G = "\033[0;32m"
Y = "\033[1;33m"
R = "\033[0;31m"
C = "\033[0;36m"
B = "\033[1;34m"
N = "\033[0m"


def hdr(msg: str) -> None:
    print(f"\n{B}══ {msg} ══{N}", flush=True)


def info(msg: str) -> None:
    print(f"{Y}[hw-e2e]{N} {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"  {G}✓{N} {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  {R}✗{N} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  {Y}!{N} {msg}", flush=True)


def prompt(msg: str) -> None:
    print(f"  {C}▶ {msg}{N}", flush=True)


_HTTP = httpx.Client(timeout=15.0, follow_redirects=True)


# ─── HTTP helpers ──────────────────────────────────────────
def api_call(method: str, path: str, *, base: str | None = None, **kw: Any) -> tuple[int, dict | str | None]:
    """(status_code, body or None) 반환. 통신 실패 시 (-1, None)."""
    url = (base or BASE_URL) + path
    try:
        r = _HTTP.request(method, url, **kw)
    except httpx.HTTPError as e:
        fail(f"{method} {url} — 통신 실패: {e}")
        return -1, None
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = r.text
    return r.status_code, body


def api_post(path: str, **kw: Any) -> dict | None:
    code, body = api_call("POST", path, **kw)
    if code >= 400 or code < 0:
        fail(f"POST {path} → {code}: {str(body)[:200]}")
        return None
    print(f"    POST  {path:60s} → {code}")
    return body if isinstance(body, dict) else None


def api_get(path: str, *, base: str | None = None) -> tuple[int, Any]:
    code, body = api_call("GET", path, base=base)
    print(f"    GET   {path:60s} → {code}")
    return code, body


# ─── DB helpers ────────────────────────────────────────────
def db_one(sql: str, **p: Any) -> Any:
    with SessionLocal() as db:
        return db.execute(text(sql), p).first()


def db_all(sql: str, **p: Any) -> list[Any]:
    with SessionLocal() as db:
        return list(db.execute(text(sql), p).all())


def poll_until(desc: str, fn, timeout: float, interval: float = 1.0) -> Any:
    """fn() 이 truthy 값을 반환할 때까지 폴링. 타임아웃 시 None."""
    start = time.monotonic()
    print(f"    waiting: {desc} (timeout={int(timeout)}s) ", end="", flush=True)
    while time.monotonic() - start < timeout:
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            print(f"\n    {R}poll error{N}: {e}")
            result = None
        if result:
            elapsed = int(time.monotonic() - start)
            print(f"[{elapsed}s] {G}✓{N}")
            return result
        time.sleep(interval)
        print(".", end="", flush=True)
    print(f" [{int(timeout)}s] {R}TIMEOUT{N}")
    return None


# ─── 사전 점검 ─────────────────────────────────────────────
def phase0_preflight(timeout: float) -> bool:
    hdr("Phase 0 / 사전 점검 (Backend / Web / 핵심 테이블)")

    # Backend openapi
    code, _ = api_get("/openapi.json")
    if code != 200:
        fail("backend /openapi.json 응답 비정상 — uvicorn 로그 확인")
        return False
    ok("backend FastAPI :8000 응답 OK")

    # Backend health (management proxy)
    code, body = api_get("/api/management/health")
    if code == 200:
        ok(f"backend → management gRPC health OK ({body if isinstance(body, dict) else ''})")
    elif code == 503:
        warn("management gRPC 503 — ROS2 어댑터 미동작일 수 있음 (계속 진행)")
    else:
        warn(f"management health 비정상 status={code} — 계속 진행")

    # Backend production GET (PyQt 가 의존)
    code, body = api_get("/api/production/items")
    if code != 200:
        fail("backend /api/production/items 비정상 — PyQt 가 사용할 API")
        return False
    ok(f"backend /api/production/items OK (현재 items={len(body) if isinstance(body, list) else '?'})")

    # Backend orders GET (Web 가 의존)
    code, body = api_get("/api/orders")
    if code != 200:
        fail("backend /api/orders 비정상 — Web /orders 페이지가 사용할 API")
        return False
    ok(f"backend /api/orders OK (현재 orders={len(body) if isinstance(body, list) else '?'})")

    # Web frontend 응답
    code, _ = api_get("/", base=WEB_URL)
    if code != 200:
        warn(f"web :3001 / 응답 status={code} — next.js 부팅 지연일 수 있음")
    else:
        ok("web Next.js :3001 / 200 OK")

    code, _ = api_get("/orders", base=WEB_URL)
    if code != 200:
        warn(f"web /orders 응답 status={code}")
    else:
        ok("web /orders 200 OK")

    # DB 핵심 테이블 존재 확인
    schema = os.environ["SMARTCAST_SCHEMA"]
    info(f"DB 스키마: {schema}")
    missing = []
    for tname in CORE_TABLES:
        try:
            db_one(f"SELECT 1 FROM {schema}.{tname} LIMIT 1")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "does not exist" in msg or "UndefinedTable" in msg:
                missing.append(tname)
    if missing:
        for t in missing:
            fail(f"{schema}.{t} 존재하지 않음")
        return False
    ok(f"{schema}.{{{', '.join(CORE_TABLES)}}} 모두 존재")
    return True


# ─── Phase 1 — 발주 생성 ───────────────────────────────────
def phase1_create_order(product: str, timeout: float) -> int | None:
    hdr("Phase 1 / 발주 생성")
    res = api_post("/api/orders/customer", json={
        "company_name": "HW E2E",
        "customer_name": "Ubuntu Dev",
        "phone": "010-0000-0000",
        "email": "hw-e2e@local",
        "shipping_address": "서울 e2e",
        "total_amount": 200000.0,
        "requested_delivery": "2026-12-31",
        "details": [{
            "product_id": product,
            "product_name": f"HW E2E {product}",
            "quantity": 1,
            "diameter": "450mm",
            "thickness": "30mm",
            "load_class": "일반",
            "material": "주철",
            "post_processing_ids": ["polish", "coat"],
            "unit_price": 200000.0,
            "subtotal": 200000.0,
        }],
    })
    if not res or "ord_id" not in res:
        return None
    ord_id = int(res["ord_id"])
    ok(f"ord_id={ord_id} 생성")

    # DB 검증: ord, ord_pattern, ord_pp_map
    row = db_one("SELECT user_id FROM ord WHERE ord_id=:o", o=ord_id)
    if not row:
        fail("ord row 미생성")
        return None
    ok(f"DB ord row OK (user_id={row.user_id})")

    pattern = db_one("SELECT ptn_loc_id FROM ord_pattern WHERE ord_id=:o", o=ord_id)
    if not pattern:
        fail("ord_pattern 미생성")
        return None
    ok(f"DB ord_pattern OK (ptn_loc_id={pattern.ptn_loc_id})")

    pp_maps = db_all("SELECT pp_id FROM ord_pp_map WHERE ord_id=:o", o=ord_id)
    ok(f"DB ord_pp_map rows={len(pp_maps)} ({[r.pp_id for r in pp_maps]})")

    # Backend API (Web/PyQt 가 의존)
    code, _ = api_get(f"/api/orders/{ord_id}")
    if code != 200:
        fail(f"/api/orders/{ord_id} 비정상 — Web 상세 페이지가 깨질 수 있음")
        return None
    ok("backend /api/orders/{id} 일치 응답")
    return ord_id


# ─── Phase 2 — APPR 전이 ───────────────────────────────────
def phase2_approve(ord_id: int) -> bool:
    hdr("Phase 2 / 발주 승인 RCVD → APPR")
    res = api_post(f"/api/orders/{ord_id}/status?new_stat=APPR")
    if res is None:
        return False
    row = db_one("SELECT ord_stat FROM ord_stat WHERE ord_id=:o", o=ord_id)
    if not row or row.ord_stat != "APPR":
        fail(f"ord_stat={row.ord_stat if row else None} (expected APPR)")
        return False
    ok("DB ord_stat=APPR")
    return True


# ─── Phase 3 — 생산 시작 ──────────────────────────────────
def phase3_start_production(ord_id: int, timeout: float) -> int | None:
    hdr("Phase 3 / 생산 시작 → item + RA1/MM 생성")
    res = api_post("/api/production/start", json={"ord_id": ord_id})
    if res is None:
        return None

    items = db_all("SELECT item_id, cur_stat FROM item WHERE ord_id=:o ORDER BY item_id", o=ord_id)
    if not items:
        fail("item row 미생성")
        return None
    item_id = int(items[0].item_id)
    ok(f"DB item_id={item_id} (cur_stat={items[0].cur_stat})")

    # MM equip_task_txn 폴링
    res = poll_until(
        "RA1/MM equip_task_txn 생성",
        lambda: db_one(
            "SELECT txn_id, task_type, txn_stat, res_id FROM equip_task_txn "
            "WHERE item_id=:i AND task_type='MM' ORDER BY txn_id DESC LIMIT 1",
            i=item_id,
        ),
        timeout,
    )
    if not res:
        return None
    ok(f"DB MM txn_id={res.txn_id} res_id={res.res_id or '?'} stat={res.txn_stat}")

    # Backend API for PyQt: equip-tasks
    code, body = api_get("/api/production/equip-tasks")
    if code != 200:
        warn("backend /api/production/equip-tasks 비정상 — PyQt 진행 현황이 깨질 수 있음")
    else:
        match = [r for r in body if isinstance(r, dict) and r.get("item_id") == item_id] if isinstance(body, list) else []
        ok(f"backend /api/production/equip-tasks 이 item 의 task 표시 ({len(match)} 건)")

    return item_id


# ─── Phase 4 — RA 자율 사슬 ─────────────────────────────────
def phase4_ra_chain(item_id: int, timeout: float) -> bool:
    hdr("Phase 4 / RA 자율 사슬 (MM → POUR → DM)")
    prompt("RA 암 컨트롤러(ROS2)가 자율적으로 MM → POUR → DM 순서로 진행합니다.")
    prompt("운영자 개입 없음 — 라인이 멈춰 있다면 RA 컨트롤러 로그 확인.")

    for tt in ["MM", "POUR", "DM"]:
        res = poll_until(
            f"{tt} SUCC 전이",
            lambda x=tt: db_one(
                "SELECT txn_id FROM equip_task_txn "
                "WHERE item_id=:i AND task_type=:t AND txn_stat='SUCC' "
                "ORDER BY txn_id DESC LIMIT 1",
                i=item_id, t=x,
            ),
            timeout,
        )
        if not res:
            fail(f"{tt} SUCC 미도달 — RA 어댑터 / state_manager 확인")
            return False
        ok(f"DB {tt} SUCC (txn_id={res.txn_id})")

    # DM SUCC 후 ToPP trans_task_txn 자동 생성
    res = poll_until(
        "ToPP trans_task_txn 자동 생성 (TAT 운반 시작)",
        lambda: db_one(
            "SELECT trans_task_txn_id, txn_stat, chg_loc_id, trans_id "
            "FROM trans_task_txn WHERE item_id=:i AND task_type='ToPP' "
            "ORDER BY trans_task_txn_id DESC LIMIT 1",
            i=item_id,
        ),
        timeout,
    )
    if not res:
        fail("ToPP trans_task_txn 미생성 — orchestrator.create_next_task 흐름 확인")
        return False
    ok(f"DB ToPP trans_task_txn_id={res.trans_task_txn_id} TAT={res.trans_id} stat={res.txn_stat}")
    return True


# ─── Phase 5 — TAT 핸드오프 ───────────────────────────────
def phase5_handoff(item_id: int, timeout: float) -> bool:
    hdr("Phase 5 / TAT 도착 + 작업자 ① 하차완료")
    prompt("아래 둘 중 하나로 핸드오프를 알려주세요:")
    prompt("  (A) PyQt pp_worker 화면 '① 하차완료' 버튼")
    prompt("  (B) 작업대 ESP32 GPIO33 빨간 버튼")

    baseline = db_one("SELECT COALESCE(MAX(log_id), 0) AS m FROM log_action_operator_handoff_acks").m

    ack = poll_until(
        "log_action_operator_handoff_acks INSERT",
        lambda: db_one(
            "SELECT log_id, item_id, button_device_id, pp_task_txn_id, operator_id "
            "FROM log_action_operator_handoff_acks "
            "WHERE log_id > :b ORDER BY log_id DESC LIMIT 1",
            b=baseline,
        ),
        timeout,
    )
    if not ack:
        fail("log_action_operator_handoff_acks 신규 row 없음 — ESP32 버튼 또는 PyQt 핸드오프 점검")
        return False
    ok(
        f"DB handoff log_id={ack.log_id} item_id={ack.item_id} "
        f"button={ack.button_device_id} operator={ack.operator_id}"
    )

    pp_rows = poll_until(
        "pp_task_txn (QUE) 자동 생성",
        lambda: db_all(
            "SELECT txn_id, pp_nm, txn_stat FROM pp_task_txn WHERE item_id=:i ORDER BY txn_id",
            i=item_id,
        ),
        timeout,
    )
    if not pp_rows:
        return False
    states = ", ".join(f"{r.pp_nm}={r.txn_stat}" for r in pp_rows)
    ok(f"DB pp_task_txn rows={len(pp_rows)}: {states}")
    return True


# ─── Phase 6 — RFID 스캔 ─────────────────────────────────
def phase6_rfid(item_id: int, timeout: float) -> bool:
    hdr("Phase 6 / 작업자 ② RFID 스캔")
    prompt("아래 둘 중 하나로 RFID 스캔을 수행해주세요:")
    prompt("  (A) RC522 리더에 NDEF Text 굽힌 태그 댐 (Jetson EventGateway RFID_SCANNED)")
    prompt("  (B) PyQt pp_worker 화면 '② 후처리 검색' 버튼 (payload 입력 후)")

    baseline = db_one("SELECT COALESCE(MAX(id), 0) AS m FROM log_action_operator_rfid_scan").m

    res = poll_until(
        "log_action_operator_rfid_scan INSERT",
        lambda: db_one(
            "SELECT id, item_id, raw_payload, parse_status FROM log_action_operator_rfid_scan "
            "WHERE id > :b ORDER BY id DESC LIMIT 1",
            b=baseline,
        ),
        timeout,
    )
    if not res:
        fail("rfid scan log 신규 row 없음 — RC522 / NDEF Text / PyQt 클라이언트 점검")
        return False
    ok(f"DB rfid id={res.id} item_id={res.item_id} parse_status={res.parse_status}")
    if res.item_id and int(res.item_id) != item_id:
        warn(f"스캔된 item_id={res.item_id} ≠ 예상 item_id={item_id} (다른 item 의 태그? 계속 진행)")
    return True


# ─── Phase 7 — PyQt ③ 후처리 완료 ─────────────────────────
def phase7_pp_done(item_id: int, timeout: float) -> bool:
    hdr("Phase 7 / PyQt ③ 후처리 완료 → CONV1 모터 ON")
    prompt("PyQt pp_worker 화면에서 '③ 후처리 완료' 버튼을 눌러주세요.")
    prompt("3 초 카운트다운 후 POST /api/management/conveyor/CONV-01/start 가 발사되고")
    prompt("ESP32 모터가 ON 되어 부품이 검사 위치로 이송됩니다.")
    prompt("(2026-05 부터 구 TOF1 진입 트리거는 이 버튼으로 대체됨)")

    res = poll_until(
        "pp_task_txn 전체 SUCC",
        lambda: db_one(
            "SELECT bool_and(txn_stat='SUCC') AS all_ok, count(*) AS n "
            "FROM pp_task_txn WHERE item_id=:i",
            i=item_id,
        ),
        timeout,
    )
    if not res or not res.all_ok:
        fail("pp_task_txn 일부 미완료 — PyQt 버튼 미발사 또는 백엔드 처리 실패")
        return False
    ok(f"DB pp_task_txn 전체 SUCC (n={res.n})")

    res = poll_until(
        "ToINSP equip_task_txn (PROC) 생성",
        lambda: db_one(
            "SELECT txn_id, txn_stat, res_id FROM equip_task_txn "
            "WHERE item_id=:i AND task_type='ToINSP' "
            "ORDER BY txn_id DESC LIMIT 1",
            i=item_id,
        ),
        timeout,
    )
    if not res:
        return False
    ok(f"DB ToINSP txn_id={res.txn_id} stat={res.txn_stat} res_id={res.res_id}")
    return True


# ─── Phase 8 — 자동 검사 ──────────────────────────────────
def phase8_inspect(item_id: int, timeout: float) -> tuple[bool, str | None, int | None]:
    """검사 + AI 추론. 반환: (성공여부, final_result, insp_txn_id)."""
    hdr("Phase 8 / 자동 검사 (TOF2 → 카메라 → AI)")
    prompt("부품이 컨베이어로 검사 위치까지 이동하면 TOF2 가 카메라를 트리거합니다.")
    prompt("Jetson 이 UploadInspectionImage RPC 로 이미지 업로드 → AI 어댑터 실행.")

    baseline_insp = db_one("SELECT COALESCE(MAX(txn_id), 0) AS m FROM insp_task_txn").m

    insp = poll_until(
        "insp_task_txn (PROC) 신규 INSERT (이미지 업로드 직후)",
        lambda: db_one(
            "SELECT txn_id, txn_stat FROM insp_task_txn "
            "WHERE txn_id > :b AND item_id=:i ORDER BY txn_id DESC LIMIT 1",
            b=baseline_insp, i=item_id,
        ),
        timeout,
    )
    if not insp:
        fail("insp_task_txn 생성 안됨 — Jetson UploadInspectionImage RPC 점검")
        return False, None, None
    insp_txn_id = int(insp.txn_id)
    ok(f"DB insp txn_id={insp_txn_id} stat={insp.txn_stat}")

    # AI 추론 완료 (1개 이상의 ai_inference_txn SUCC)
    ai = poll_until(
        "ai_inference_txn (SUCC) 1건 이상",
        lambda: db_one(
            "SELECT count(*) AS n FROM ai_inference_txn "
            "WHERE insp_txn_id=:t AND txn_stat='SUCC'",
            t=insp_txn_id,
        ),
        timeout,
    )
    if not ai or ai.n < 1:
        fail("AI 추론 미완료")
        return False, None, insp_txn_id
    ok(f"DB ai_inference_txn SUCC 개수={ai.n}")

    # insp_task_txn 최종 + insp_stat (final_result) + ai_inference_txn (predicted_class/confidence/anomaly)
    final = poll_until(
        "insp_task_txn 최종 (SUCC/FAIL) + insp_stat (final_result)",
        lambda: db_one(
            "SELECT it.txn_stat, it.result, st.final_result, "
            "       st.yolo_inference_id, st.patchcore_inference_id "
            "FROM insp_task_txn it LEFT JOIN insp_stat st ON st.insp_txn_id=it.txn_id "
            "WHERE it.txn_id=:t AND it.txn_stat IN ('SUCC', 'FAIL')",
            t=insp_txn_id,
        ),
        timeout,
    )
    if not final:
        return False, None, insp_txn_id

    # ai_inference_txn 상세 (YOLO + PatchCore 각각)
    inferences = db_all(
        "SELECT inference_id, step_type, txn_stat, predicted_class, "
        "       confidence, anomaly_score, is_anomaly "
        "FROM ai_inference_txn WHERE insp_txn_id=:t ORDER BY inference_id",
        t=insp_txn_id,
    )
    for inf in inferences:
        ok(
            f"  AI [{inf.step_type}] inf_id={inf.inference_id} stat={inf.txn_stat} "
            f"class={inf.predicted_class} conf={inf.confidence} "
            f"anomaly={inf.anomaly_score} is_anomaly={inf.is_anomaly}"
        )

    ok(
        f"DB insp 최종 stat={final.txn_stat} result={final.result} "
        f"final_result={final.final_result}"
    )
    return True, final.final_result, insp_txn_id


# ─── Phase 9 — 결과 분기 ──────────────────────────────────
def phase9_branch(ord_id: int, item_id: int, final_result: str | None,
                  timeout: float) -> tuple[bool, int | None]:
    """결과별 분기. defective 시 새 replacement item_id 반환."""
    hdr(f"Phase 9 / 결과 분기 (final_result={final_result})")

    if final_result == "GP":
        prompt("양품 — ToPAWait → ToSTRG → PA_GP → item DONE 흐름 폴링")
        res = poll_until(
            "ToPAWait/PA_GP/PICK equip_task_txn 등장",
            lambda: db_one(
                "SELECT task_type, txn_stat FROM equip_task_txn "
                "WHERE item_id=:i AND task_type IN ('ToPAWait','PA_GP','PICK') "
                "ORDER BY txn_id DESC LIMIT 1",
                i=item_id,
            ),
            timeout,
        )
        if not res:
            warn("post-INSP equip_task 미생성 — orchestrator GP flow 점검")
            return False, None
        ok(f"DB post-INSP task={res.task_type} stat={res.txn_stat}")

        res = poll_until(
            "item.cur_stat 최종 전이 (DONE/SUCC/OUT)",
            lambda: db_one(
                "SELECT cur_stat FROM item WHERE item_id=:i",
                i=item_id,
            ),
            timeout,
            interval=2.0,
        )
        ok(f"DB item.cur_stat={res.cur_stat if res else 'None'} (양품 흐름 진행 중/완료)")
        return True, None

    if final_result == "DP":
        prompt("불량 — tostrg_dld 이벤트 후 대체 item 생성 + RA 재진입 폴링")
        res = poll_until(
            "item.is_defective=True 설정",
            lambda: db_one(
                "SELECT is_defective FROM item WHERE item_id=:i AND is_defective=TRUE",
                i=item_id,
            ),
            timeout,
        )
        if not res:
            warn("item.is_defective 미설정 — orchestrator INSP FAIL 흐름 점검")
        else:
            ok("DB item.is_defective=True")

        repl = poll_until(
            "대체 item 생성 (RA 재진입)",
            lambda: db_one(
                "SELECT item_id, cur_stat FROM item "
                "WHERE ord_id=:o AND item_id != :i "
                "ORDER BY item_id DESC LIMIT 1",
                o=ord_id, i=item_id,
            ),
            timeout,
        )
        if not repl:
            fail("대체 item 미생성 — task_manager.create_next_task 'tostrg_dld' 흐름 점검")
            return False, None
        repl_item_id = int(repl.item_id)
        ok(f"DB 대체 item_id={repl_item_id} (cur_stat={repl.cur_stat}) — RA 재진입!")

        mm = poll_until(
            "대체 item 의 새 MM equip_task_txn (RA → TAT → CONV1 사이클 재시작)",
            lambda: db_one(
                "SELECT txn_id, txn_stat FROM equip_task_txn "
                "WHERE item_id=:i AND task_type='MM' "
                "ORDER BY txn_id DESC LIMIT 1",
                i=repl_item_id,
            ),
            timeout,
        )
        if not mm:
            fail("대체 item 에 MM 태스크 미생성")
            return False, repl_item_id
        ok(f"DB 대체 MM txn_id={mm.txn_id} stat={mm.txn_stat} — RA 사이클 재진입 OK")

        padp = db_one(
            "SELECT txn_id, txn_stat FROM equip_task_txn "
            "WHERE item_id=:i AND task_type='PA_DP' "
            "ORDER BY txn_id DESC LIMIT 1",
            i=item_id,
        )
        if padp:
            ok(f"DB 불량품 PA_DP txn_id={padp.txn_id} stat={padp.txn_stat}")
        else:
            warn("PA_DP 태스크 미생성")
        return True, repl_item_id

    warn(f"final_result={final_result} — 알 수 없는 분기, 검증 건너뜀")
    return True, None


# ─── 최종 일관성 ───────────────────────────────────────────
def final_consistency(ord_id: int, item_id: int, replacement_id: int | None) -> bool:
    hdr("Phase 10 / 최종 일관성 검증")

    r = db_one(
        """
        SELECT
          (SELECT cur_stat FROM item WHERE item_id=:i) AS item_stat,
          (SELECT count(*) FROM equip_task_txn WHERE item_id=:i AND txn_stat='SUCC') AS equip_succ,
          (SELECT count(*) FROM equip_task_txn WHERE item_id=:i) AS equip_total,
          (SELECT count(*) FROM pp_task_txn    WHERE item_id=:i AND txn_stat='SUCC') AS pp_succ,
          (SELECT count(*) FROM insp_task_txn  WHERE item_id=:i AND txn_stat IN ('SUCC','FAIL')) AS insp_done,
          (SELECT ord_stat FROM ord_stat WHERE ord_id=:o) AS ord_stat
        """,
        i=item_id, o=ord_id,
    )
    print(f"    item.cur_stat       = {r.item_stat}")
    print(f"    equip_task SUCC     = {r.equip_succ} / total={r.equip_total}")
    print(f"    pp_task SUCC        = {r.pp_succ}")
    print(f"    insp_task done      = {r.insp_done}")
    print(f"    ord_stat            = {r.ord_stat}")

    issues = []
    if r.equip_succ < 3:
        issues.append(f"equip SUCC={r.equip_succ} (<3: MM/POUR/DM 필수)")
    if r.pp_succ < 1:
        issues.append(f"pp SUCC={r.pp_succ}")
    if r.insp_done < 1:
        issues.append(f"insp done={r.insp_done}")

    if replacement_id:
        rr = db_one(
            "SELECT cur_stat FROM item WHERE item_id=:i",
            i=replacement_id,
        )
        print(f"    replacement_item_id = {replacement_id} (cur_stat={rr.cur_stat if rr else None})")
        repl_mm = db_one(
            "SELECT count(*) AS n FROM equip_task_txn "
            "WHERE item_id=:i AND task_type='MM'",
            i=replacement_id,
        )
        if not repl_mm or repl_mm.n < 1:
            issues.append("대체 item 에 MM 태스크 없음")

    if issues:
        for i in issues:
            fail(i)
        return False
    ok("4-table 일관성 OK")
    return True


# ─── cleanup ──────────────────────────────────────────────
def cleanup(ord_id: int, item_ids: list[int]) -> None:
    """FK 의존 순서를 지켜 자식 → 부모 순으로 정리.

    item 을 참조하는 테이블 (smartcast 스키마, 2026-05 기준):
      insp_stat / ai_inference_txn / insp_task_txn / log_handoff / pp_task_txn /
      trans_task_txn / trans_stat / equip_task_txn / equip_stat /
      strg_location_stat / ship_location_stat / log_rfid_scan
    slot 류 (strg/ship_location_stat) 는 row 삭제 대신 item_id NULL 로 분리.
    """
    hdr("Cleanup / 테스트 row 삭제")
    schema = os.environ["SMARTCAST_SCHEMA"]
    # 자식 → 부모 순 (insp_stat 가 ai_inference_txn 도 참조하므로 가장 먼저)
    stmts = [
        ("insp_stat",        "DELETE FROM {s}.insp_stat        WHERE item_id = ANY(:ids)"),
        ("ai_inference_txn", "DELETE FROM {s}.ai_inference_txn WHERE insp_txn_id IN "
                             "(SELECT txn_id FROM {s}.insp_task_txn WHERE item_id = ANY(:ids))"),
        ("insp_task_txn",    "DELETE FROM {s}.insp_task_txn    WHERE item_id = ANY(:ids)"),
        ("handoff",          "DELETE FROM {s}.log_action_operator_handoff_acks "
                             "WHERE item_id = ANY(:ids)"),
        ("pp_task_txn",      "DELETE FROM {s}.pp_task_txn      WHERE item_id = ANY(:ids)"),
        ("trans_stat",       "DELETE FROM {s}.trans_stat       WHERE item_id = ANY(:ids)"),
        ("trans_task_txn",   "DELETE FROM {s}.trans_task_txn   WHERE item_id = ANY(:ids)"),
        ("equip_stat",       "DELETE FROM {s}.equip_stat       WHERE item_id = ANY(:ids)"),
        ("equip_task_txn",   "DELETE FROM {s}.equip_task_txn   WHERE item_id = ANY(:ids)"),
        ("rfid",             "DELETE FROM {s}.log_action_operator_rfid_scan "
                             "WHERE item_id = ANY(:ids)"),
        # slot 류는 row 보존, item_id NULL 처리 (FK SET NULL 효과)
        ("strg_loc_unbind",  "UPDATE {s}.strg_location_stat   SET item_id=NULL "
                             "WHERE item_id = ANY(:ids)"),
        ("ship_loc_unbind",  "UPDATE {s}.ship_location_stat   SET item_id=NULL "
                             "WHERE item_id = ANY(:ids)"),
        ("item",             "DELETE FROM {s}.item             WHERE item_id = ANY(:ids)"),
        ("ord_pp_map",       "DELETE FROM {s}.ord_pp_map       WHERE ord_id=:o"),
        ("ord_pattern",      "DELETE FROM {s}.ord_pattern      WHERE ord_id=:o"),
        ("ord_stat",         "DELETE FROM {s}.ord_stat         WHERE ord_id=:o"),
        ("ord",              "DELETE FROM {s}.ord              WHERE ord_id=:o"),
    ]
    with SessionLocal() as db:
        for label, tmpl in stmts:
            sql = tmpl.format(s=schema)
            try:
                db.execute(text(sql), {"ids": item_ids, "o": ord_id})
            except Exception as e:  # noqa: BLE001
                warn(f"{label} skip ({e.__class__.__name__}: {str(e)[:80]})")
        db.commit()
    ok(f"cleanup 완료 (ord_id={ord_id} items={item_ids})")


# ─── main ──────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="HW Full E2E watcher (RA → TAT → CONV1 → RA)")
    p.add_argument("--product", default=DEFAULT_PRODUCT)
    p.add_argument("--timeout", type=float, default=300.0,
                   help="단계별 폴링 타임아웃 (초). 기본 300s")
    p.add_argument("--keep-data", action="store_true",
                   help="성공 후 테스트 row 보존")
    p.add_argument("--skip-preflight", action="store_true",
                   help="Phase 0 사전 점검 생략 (디버깅용)")
    args = p.parse_args()

    started = datetime.now(UTC)
    print(f"{B}{'=' * 70}{N}")
    print(f"{B}  HW Full E2E — RA → TAT → CONV1 → (불량 시) RA 재진입{N}")
    print(f"{B}  start={started.isoformat()}  product={args.product}{N}")
    print(f"{B}  timeout/단계={int(args.timeout)}s  keep={args.keep_data}{N}")
    print(f"{B}{'=' * 70}{N}")

    if not args.skip_preflight:
        if not phase0_preflight(args.timeout):
            return 10

    ord_id = phase1_create_order(args.product, args.timeout)
    if not ord_id:
        return 11
    if not phase2_approve(ord_id):
        return 12
    item_id = phase3_start_production(ord_id, args.timeout)
    if not item_id:
        return 13
    if not phase4_ra_chain(item_id, args.timeout):
        return 14
    if not phase5_handoff(item_id, args.timeout):
        return 15
    if not phase6_rfid(item_id, args.timeout):
        return 16
    if not phase7_pp_done(item_id, args.timeout):
        return 17
    ok_insp, final_result, _ = phase8_inspect(item_id, args.timeout)
    if not ok_insp:
        return 18
    branch_ok, replacement_id = phase9_branch(ord_id, item_id, final_result, args.timeout)
    if not branch_ok:
        return 19

    final_ok = final_consistency(ord_id, item_id, replacement_id)

    if not args.keep_data:
        all_items = [item_id]
        if replacement_id:
            all_items.append(replacement_id)
        cleanup(ord_id, all_items)

    elapsed = (datetime.now(UTC) - started).total_seconds()
    print()
    if final_ok:
        print(f"{G}{'=' * 70}{N}")
        print(f"{G}✅ HW Full E2E 통과 — ord_id={ord_id} item_id={item_id} "
              f"replacement={replacement_id} ({elapsed:.1f}s){N}")
        print(f"{G}{'=' * 70}{N}")
        return 0

    print(f"{R}{'=' * 70}{N}")
    print(f"{R}❌ HW Full E2E 실패 (일관성 검증) — ord_id={ord_id} item_id={item_id}{N}")
    print(f"{R}{'=' * 70}{N}")
    return 20


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{Y}[hw-e2e] 사용자 중단 (Ctrl+C){N}")
        sys.exit(130)
