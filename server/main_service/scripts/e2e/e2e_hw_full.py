"""HW 풀 e2e 워처 — RA + TAT + CONV1 전체 사슬 단계별 폴링.

각 단계마다:
  1) 운영자에게 다음 액션 안내를 출력
  2) DB 의 기대 상태 전이를 1초 간격 폴링
  3) timeout (--timeout, 기본 300s) 초과 시 실패로 종료

폴링 키 (smartcast schema):
  Phase 1  ord                 — POST /api/orders/customer 호출 직후 ord row 생성
  Phase 2  ord_stat            — APPR 전이 (API 호출)
  Phase 3  item                — POST /api/production/start, RA1/MM equip_task_txn(QUE)
  Phase 4  equip_task_txn      — MM → POUR → DM → ToPP advance (PyQt 운영자)
  Phase 5  trans_task_txn,
           handoff_acks        — TAT ToPP 도착 + 핸드오프 버튼 (실 HW)
  Phase 6  rfid_scan_log       — 작업자 RFID 스캔 (실 HW)
  Phase 7  pp_task_txn,
           equip_task_txn      — PyQt "③ 후처리 완료" 버튼 → 3 초 카운트다운 →
                                 POST /api/management/conveyor/CONV-01/start →
                                 ESP32 모터 ON → pp_task_txn SUCC + ToINSP PROC.
                                 (구 TOF1_ENTRY 트리거는 2026-05 폐기됨)
  Phase 8  insp_task_txn,
           ai_inference_txn    — 컨베이어 운반 후 검사 위치 정지 → Jetson 카메라
                                 자동 캡처 → UploadInspectionImage RPC →
                                 INSP_IMAGE_UPLOADED → AI 추론
  Phase 9  item, equip_task    — OUT 전이 (PyQt 운영자)

기본적으로 검증 완료 후 ord/item/관련 txn row 정리 (--keep-data 로 보존 가능).
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
    print("✗ DATABASE_URL 미설정 — .env.local 자동 로드 후 재실행")
    sys.exit(2)

# 기본 스키마 — RDS 는 public, 로컬 Tailscale 은 smartcast
os.environ.setdefault("SMARTCAST_SCHEMA", os.environ.get("PG_SCHEMA", "public"))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from smart_cast_db.database import SessionLocal  # noqa: E402


_HTTP_CLIENT = httpx.Client(timeout=15.0)


BASE_URL = os.environ.get("E2E_BACKEND_URL", "http://127.0.0.1:8000")
DEFAULT_PRODUCT = "R-D450"

G = "\033[0;32m"
Y = "\033[1;33m"
R = "\033[0;31m"
C = "\033[0;36m"
N = "\033[0m"


def info(msg: str) -> None:
    print(f"{Y}[hw-e2e]{N} {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"{G}  ✓{N} {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"{R}  ✗{N} {msg}", flush=True)


def prompt(msg: str) -> None:
    print(f"{C}  ▶ {msg}{N}", flush=True)


# ─── API helpers ────────────────────────────────────────────
def api(method: str, path: str, **kw: Any) -> dict | None:
    url = BASE_URL + path
    try:
        r = _HTTP_CLIENT.request(method, url, **kw)
    except httpx.HTTPError as e:
        fail(f"{method} {path} — 통신 실패: {e}")
        return None
    if r.status_code >= 400:
        fail(f"{method} {path} → {r.status_code}: {r.text[:200]}")
        return None
    print(f"    {method:5s} {path:60s} → {r.status_code}")
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return None


# ─── DB helpers ─────────────────────────────────────────────
def db_one(sql: str, **p: Any) -> Any:
    with SessionLocal() as db:
        return db.execute(text(sql), p).first()


def db_all(sql: str, **p: Any) -> list[Any]:
    with SessionLocal() as db:
        return list(db.execute(text(sql), p).all())


def poll_until(
    desc: str,
    fn,
    timeout: float,
    interval: float = 1.0,
):
    """fn() 이 truthy 값을 반환할 때까지 폴링. 타임아웃 시 None."""
    start = time.monotonic()
    last_shown = ""
    print(f"    waiting: {desc} (timeout={int(timeout)}s)", end="", flush=True)
    while time.monotonic() - start < timeout:
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            print(f"\n{R}    poll error: {e}{N}")
            result = None
        if result:
            elapsed = int(time.monotonic() - start)
            print(f"  [{elapsed}s] {G}✓{N}")
            return result
        # 점 진행 표시
        elapsed = int(time.monotonic() - start)
        marker = f"  [{elapsed}s]"
        if marker != last_shown:
            print(".", end="", flush=True)
            last_shown = marker
        time.sleep(interval)
    print(f"  [{int(timeout)}s] {R}TIMEOUT{N}")
    return None


# ─── Phase 구현 ─────────────────────────────────────────────
def phase1_create_order(product: str) -> int | None:
    info("[Phase 1] 고객 발주 생성")
    res = api("POST", "/api/orders/customer", json={
        "company_name": "HW E2E Test",
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
    ok(f"ord_id={ord_id}")
    return ord_id


def phase2_approve(ord_id: int) -> bool:
    info("[Phase 2] 발주 승인 RCVD → APPR")
    res = api("POST", f"/api/orders/{ord_id}/status?new_stat=APPR")
    if res is None:
        return False
    row = db_one(
        "SELECT ord_stat FROM ord_stat WHERE ord_id=:o ORDER BY stat_id DESC LIMIT 1",
        o=ord_id,
    )
    if row and row.ord_stat == "APPR":
        ok(f"ord_stat=APPR")
        return True
    fail(f"ord_stat 전이 실패: {row}")
    return False


def phase3_start_production(ord_id: int, timeout: float) -> int | None:
    info("[Phase 3] 라인 투입 — Item + RA1/MM equip_task_txn 생성")
    res = api("POST", "/api/production/start", json={"ord_id": ord_id})
    if res is None:
        return None
    items = db_all(
        "SELECT item_id, cur_stat FROM item WHERE ord_id=:o ORDER BY item_id",
        o=ord_id,
    )
    if not items:
        fail("item 생성 안됨")
        return None
    item_id = int(items[0].item_id)
    ok(f"item_id={item_id} (cur_stat={items[0].cur_stat})")

    # RA1/MM equip_task_txn 가 QUE 또는 PROC 로 생성되었는지 폴링
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
    ok(f"MM txn_id={res.txn_id}  res_id={res.res_id}  stat={res.txn_stat}")
    return item_id


def phase4_ra_advance(item_id: int, timeout: float) -> int | None:
    info("[Phase 4] RA 공정 사슬 — MM → POUR → DM → ToPP advance")
    prompt("PyQt 운영자 화면에서 각 task 의 '진행' 버튼을 순서대로 눌러주세요.")
    prompt("자동 advance 가 설정되어 있으면 자동으로 진행됩니다.")

    expected_chain = ["MM", "POUR", "DM"]
    for task_type in expected_chain:
        res = poll_until(
            f"{task_type} SUCC 전이",
            lambda tt=task_type: db_one(
                "SELECT txn_id, txn_stat FROM equip_task_txn "
                "WHERE item_id=:i AND task_type=:t AND txn_stat='SUCC' "
                "ORDER BY txn_id DESC LIMIT 1",
                i=item_id, t=tt,
            ),
            timeout,
        )
        if not res:
            fail(f"{task_type} SUCC 미도달")
            return None
        ok(f"{task_type} SUCC (txn_id={res.txn_id})")

    # ToPP trans_task_txn 가 생성됐는지 확인 (운반 단계 시작)
    res = poll_until(
        "ToPP trans_task_txn 생성 (TAT 운반 시작)",
        lambda: db_one(
            "SELECT trans_task_txn_id, task_type, txn_stat, chg_loc_id "
            "FROM trans_task_txn WHERE item_id=:i AND task_type='ToPP' "
            "ORDER BY trans_task_txn_id DESC LIMIT 1",
            i=item_id,
        ),
        timeout,
    )
    if not res:
        return None
    ok(f"ToPP trans_task_txn_id={res.trans_task_txn_id} (chg_loc_id={res.chg_loc_id})")
    return int(res.trans_task_txn_id)


def phase5_handoff(item_id: int, timeout: float) -> bool:
    info("[Phase 5] TAT 핸드오프 ACK")
    prompt("아래 둘 중 하나로 핸드오프를 알려주세요:")
    prompt("  (A) PyQt pp_worker 화면 '① 하차완료' 버튼")
    prompt("  (B) 작업대 ESP32 GPIO33 빨간 버튼")

    # handoff_acks 신규 row + pp_task_txn(QUE) 생성 동시 확인
    baseline_id = db_one("SELECT COALESCE(MAX(id), 0) AS m FROM handoff_acks").m

    res = poll_until(
        "handoff_acks INSERT (HANDOFF_ACK)",
        lambda: db_one(
            "SELECT id, task_id, zone, ack_source FROM handoff_acks "
            "WHERE id > :b ORDER BY id DESC LIMIT 1",
            b=baseline_id,
        ),
        timeout,
    )
    if not res:
        fail("handoff_acks 신규 row 없음 — ESP32 버튼 또는 Jetson EventGateway 점검")
        return False
    ok(f"handoff_acks id={res.id} zone={res.zone} source={res.ack_source}")

    # ToPP 전이 + pp_task_txn QUE 생성
    pp_rows = poll_until(
        "pp_task_txn(QUE) 생성",
        lambda: db_all(
            "SELECT txn_id, pp_nm, txn_stat FROM pp_task_txn WHERE item_id=:i",
            i=item_id,
        ),
        timeout,
    )
    if not pp_rows:
        return False
    ok(f"pp_task_txn rows={len(pp_rows)}: " + ", ".join(
        f"{r.pp_nm}={r.txn_stat}" for r in pp_rows
    ))
    return True


def phase6_rfid(ord_id: int, item_id: int, timeout: float) -> bool:
    info("[Phase 6] 작업자 RFID 스캔")
    prompt("아래 둘 중 하나로 RFID 스캔을 수행해주세요:")
    prompt("  (A) 작업대 RC522 리더에 NDEF Text 가 굽힌 태그를 댐")
    prompt("  (B) PyQt pp_worker 화면 '② 후처리 검색' 버튼 (payload 입력 후)")

    baseline_id = db_one("SELECT COALESCE(MAX(id), 0) AS m FROM rfid_scan_log").m

    res = poll_until(
        "rfid_scan_log INSERT",
        lambda: db_one(
            "SELECT id, item_id, raw_payload, parse_status "
            "FROM rfid_scan_log WHERE id > :b ORDER BY id DESC LIMIT 1",
            b=baseline_id,
        ),
        timeout,
    )
    if not res:
        fail("rfid_scan_log 신규 row 없음 — RC522 리더 / NDEF Text 점검")
        return False
    ok(f"rfid id={res.id} item_id={res.item_id} parse_status={res.parse_status}")
    if res.item_id != item_id:
        fail(f"스캔된 item_id={res.item_id} ≠ 예상 item_id={item_id} — 잘못된 태그?")
        return False
    return True


def phase7_pp_done(item_id: int, timeout: float) -> bool:
    info("[Phase 7] PyQt '③ 후처리 완료' 버튼 → 컨베이어 진행 + ToINSP 전이")
    prompt("PyQt pp_worker 화면에서 '③ 후처리 완료' 버튼을 눌러주세요.")
    prompt("3 초 카운트다운 후 POST /api/management/conveyor/CONV-01/start 가 발사되고")
    prompt("ESP32 모터가 ON 되어 부품이 검사 위치로 이송됩니다.")
    prompt("(2026-05 부터 구 TOF1 진입 트리거는 이 버튼으로 대체됨)")

    # 모든 pp_task_txn 가 SUCC 가 됐는지 폴링
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
        fail("pp_task_txn 일부 미완료")
        return False
    ok(f"pp_task_txn n={res.n} 전체 SUCC")

    # ToINSP equip_task_txn PROC 생성
    res = poll_until(
        "ToINSP equip_task_txn(PROC) 생성",
        lambda: db_one(
            "SELECT txn_id, txn_stat, res_id FROM equip_task_txn "
            "WHERE item_id=:i AND task_type='ToINSP' AND txn_stat IN ('QUE','PROC') "
            "ORDER BY txn_id DESC LIMIT 1",
            i=item_id,
        ),
        timeout,
    )
    if not res:
        return False
    ok(f"ToINSP txn_id={res.txn_id} stat={res.txn_stat} res={res.res_id}")
    return True


def phase8_inspect(item_id: int, timeout: float) -> tuple[bool, int | None]:
    info("[Phase 8] CONV1 TOF2 → 카메라 캡처 → AI 추론")
    prompt("부품이 TOF2 위치에 도달하면 카메라가 자동 캡처합니다.")

    baseline_insp = db_one("SELECT COALESCE(MAX(txn_id), 0) AS m FROM insp_task_txn").m
    baseline_inf = db_one(
        "SELECT COALESCE(MAX(inference_id), 0) AS m FROM ai_inference_txn"
    ).m

    # insp_task_txn PROC 생성 (Jetson 의 UploadInspectionImage 직후)
    insp = poll_until(
        "insp_task_txn(PROC) 신규 INSERT",
        lambda: db_one(
            "SELECT txn_id, item_id, txn_stat, image_url FROM insp_task_txn "
            "WHERE txn_id > :b AND item_id=:i ORDER BY txn_id DESC LIMIT 1",
            b=baseline_insp, i=item_id,
        ),
        timeout,
    )
    if not insp:
        fail("insp_task_txn 생성 안됨 — Jetson UploadInspectionImage 점검")
        return False, None
    ok(f"insp txn_id={insp.txn_id} image_url={insp.image_url}")

    # ai_inference_txn 생성 + SUCC/FAIL
    ai_done = poll_until(
        "ai_inference_txn 완료 (SUCC/FAIL)",
        lambda: db_one(
            "SELECT inference_id, inference_stat, predicted_class, final_result "
            "FROM ai_inference_txn WHERE inference_id > :b "
            "ORDER BY inference_id DESC LIMIT 1",
            b=baseline_inf,
        ),
        timeout,
    )
    if not ai_done or ai_done.inference_stat not in ("SUCC", "FAIL"):
        fail("AI 추론 미완료")
        return False, insp.txn_id
    ok(
        f"AI inference_id={ai_done.inference_id} "
        f"stat={ai_done.inference_stat} class={ai_done.predicted_class} "
        f"final={ai_done.final_result}"
    )

    # insp_task_txn 최종 전이
    final_insp = poll_until(
        "insp_task_txn 최종 전이 (SUCC/FAIL)",
        lambda: db_one(
            "SELECT txn_id, txn_stat, result FROM insp_task_txn "
            "WHERE txn_id=:t AND txn_stat IN ('SUCC','FAIL')",
            t=insp.txn_id,
        ),
        timeout,
    )
    if not final_insp:
        return False, insp.txn_id
    ok(f"insp 최종 txn_stat={final_insp.txn_stat} result={final_insp.result}")
    return True, insp.txn_id


def phase9_out(item_id: int, timeout: float) -> bool:
    info("[Phase 9] 출하 OUT 전이")
    prompt("PyQt 운영자가 검사 완료 후 OUT 단계로 진행해주세요.")

    res = poll_until(
        "item.cur_stat=OUT",
        lambda: db_one(
            "SELECT cur_stat FROM item WHERE item_id=:i",
            i=item_id,
        ),
        timeout,
        interval=2.0,
    )
    if not res or res.cur_stat != "OUT":
        fail(f"item.cur_stat={res.cur_stat if res else None} (OUT 미도달)")
        return False
    ok(f"item.cur_stat=OUT")
    return True


# ─── 최종 일관성 검증 ───────────────────────────────────────
def final_consistency(ord_id: int, item_id: int) -> bool:
    info("[Final] 4-table 일관성 검증")
    rows = db_all(
        """
        SELECT
          (SELECT cur_stat FROM item WHERE item_id=:i) AS item_stat,
          (SELECT count(*) FROM equip_task_txn WHERE item_id=:i AND txn_stat='SUCC') AS equip_succ,
          (SELECT count(*) FROM pp_task_txn    WHERE item_id=:i AND txn_stat='SUCC') AS pp_succ,
          (SELECT count(*) FROM insp_task_txn  WHERE item_id=:i AND txn_stat='SUCC') AS insp_succ,
          (SELECT ord_stat FROM ord_stat WHERE ord_id=:o ORDER BY stat_id DESC LIMIT 1) AS ord_stat
        """,
        i=item_id, o=ord_id,
    )
    if not rows:
        fail("일관성 SELECT 결과 없음")
        return False
    r = rows[0]
    print(f"    item.cur_stat = {r.item_stat}")
    print(f"    equip_task_txn SUCC = {r.equip_succ}")
    print(f"    pp_task_txn SUCC = {r.pp_succ}")
    print(f"    insp_task_txn SUCC = {r.insp_succ}")
    print(f"    ord_stat = {r.ord_stat}")

    issues = []
    if r.item_stat != "OUT":
        issues.append(f"item.cur_stat={r.item_stat} (expected OUT)")
    if r.equip_succ < 3:
        issues.append(f"equip SUCC={r.equip_succ} (<3: MM/POUR/DM)")
    if r.pp_succ < 1:
        issues.append(f"pp SUCC={r.pp_succ} (<1)")
    if r.insp_succ < 1:
        issues.append(f"insp SUCC={r.insp_succ} (<1)")
    if issues:
        for i in issues:
            fail(i)
        return False
    ok("4-table 모두 OK")
    return True


# ─── cleanup ───────────────────────────────────────────────
def cleanup_test_rows(ord_id: int, item_id: int) -> None:
    info("[Cleanup] 테스트 row 삭제")
    statements = [
        "DELETE FROM ai_inference_txn WHERE inference_id IN "
        "(SELECT inference_id FROM insp_task_txn WHERE item_id=:i)",
        "DELETE FROM insp_task_txn  WHERE item_id=:i",
        "DELETE FROM pp_task_txn    WHERE item_id=:i",
        "DELETE FROM equip_task_txn WHERE item_id=:i",
        "DELETE FROM trans_task_txn WHERE item_id=:i",
        "DELETE FROM rfid_scan_log  WHERE item_id=:i",
        "DELETE FROM item_stat      WHERE item_id=:i",
        "DELETE FROM item           WHERE item_id=:i",
        "DELETE FROM ord_pp_map     WHERE ord_id=:o",
        "DELETE FROM ord_stat       WHERE ord_id=:o",
        "DELETE FROM pattern        WHERE ptn_id=:o",
        "DELETE FROM ord            WHERE ord_id=:o",
    ]
    with SessionLocal() as db:
        for stmt in statements:
            try:
                db.execute(text(stmt), {"i": item_id, "o": ord_id})
            except Exception as e:  # noqa: BLE001
                print(f"    skip ({e.__class__.__name__})")
        db.commit()
    ok("cleanup 완료")


# ─── main ──────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="HW Full E2E watcher")
    p.add_argument("--product", default=DEFAULT_PRODUCT,
                   help="발주 product_id (예: R-D450)")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="단계별 폴링 타임아웃 (초). 기본 300s")
    p.add_argument("--keep-data", action="store_true",
                   help="성공 후 테스트 row 보존")
    args = p.parse_args()

    started = datetime.now(UTC).isoformat()
    print("=" * 64)
    print(f"  HW Full E2E  start={started}")
    print(f"  product={args.product}  timeout={args.timeout}s  "
          f"keep={args.keep_data}")
    print("=" * 64)

    ord_id = phase1_create_order(args.product)
    if not ord_id:
        return 11
    if not phase2_approve(ord_id):
        return 12
    item_id = phase3_start_production(ord_id, args.timeout)
    if not item_id:
        return 13
    if not phase4_ra_advance(item_id, args.timeout):
        return 14
    if not phase5_handoff(item_id, args.timeout):
        return 15
    if not phase6_rfid(ord_id, item_id, args.timeout):
        return 16
    if not phase7_pp_done(item_id, args.timeout):
        return 17
    ok_insp, _ = phase8_inspect(item_id, args.timeout)
    if not ok_insp:
        return 18
    if not phase9_out(item_id, args.timeout):
        return 19
    if not final_consistency(ord_id, item_id):
        return 20

    if not args.keep_data:
        cleanup_test_rows(ord_id, item_id)

    elapsed = (datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds()
    print()
    print(f"{G}✅ HW Full E2E 통과 — ord_id={ord_id} item_id={item_id} "
          f"({elapsed:.1f}s){N}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{Y}[hw-e2e] 사용자 중단 (Ctrl+C){N}")
        sys.exit(130)
