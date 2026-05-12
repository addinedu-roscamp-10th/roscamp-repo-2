"""PyQt UI 통합 테스트용 종합 Mock 데이터 생성

목적:
    'pattern_control' 탭 검증에 필요한 다양한 상태의 주문 데이터를 한 번에 생성.

생성 데이터:
    [APPR + ptn_loc_id 없음] 3건 → _unreg_list (패턴 미등록)
    [APPR + ptn_loc_id 있음] 4건 → _reg_list   (패턴 등록완료, 우선순위 계산 가능)
    [MFG 상태]               2건 → 어느 리스트에도 미표시 (realistic 데이터)

실행:
    cd server/smart_cast_db
    python scripts/09_seed_mock_data.py

    # 이전 실행 데이터 삭제 후 재생성:
    python scripts/09_seed_mock_data.py --clean

확인:
    1. ./scripts/run-backend.sh  (Management gRPC :50051)
    2. ./scripts/run-pyqt.sh
    3. 첫 탭 '패턴 위치 조작 및 생산 시작' 에서:
       - 패턴 미등록 영역: 3건 표시
       - 생산 계획 영역:   4건 표시 (선택 후 우선순위 계산 가능)
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

import _db

# ─── 시나리오 정의 ────────────────────────────────────────────────────────────
# due_days: 오늘로부터 납기까지 남은 일수 (양수 = 미래, 음수 = 이미 지남)
# ptn_loc_id: None → 패턴 미등록(unreg_list), 1~3 → 등록완료(reg_list)
# mfg: True → 생산 시작(MFG) 상태까지 전환

_UNREG_SCENARIOS = [
    # 납기 긴급 (D-2) — 패턴 미등록
    {"prod_id": 1, "pp_ids": [1, 2], "qty": 10, "due_days": 2,  "user_id": 4, "ptn_loc_id": None},
    # 납기 보통 (D-10) — 패턴 미등록
    {"prod_id": 4, "pp_ids": [3],    "qty": 5,  "due_days": 10, "user_id": 5, "ptn_loc_id": None},
    # 납기 여유 (D-20) — 패턴 미등록
    {"prod_id": 7, "pp_ids": [],     "qty": 20, "due_days": 20, "user_id": 4, "ptn_loc_id": None},
]

_REG_SCENARIOS = [
    # 납기 극긴급 (D+1) — ptn_loc_id=1 → 최고 우선순위 예상
    {"prod_id": 2, "pp_ids": [1],       "qty": 50, "due_days": 1,  "user_id": 4, "ptn_loc_id": 1},
    # 납기 긴급 (D+5) — ptn_loc_id=2
    {"prod_id": 1, "pp_ids": [1, 2, 3], "qty": 30, "due_days": 5,  "user_id": 5, "ptn_loc_id": 2},
    # 납기 중간 (D+9) — ptn_loc_id=3
    {"prod_id": 9, "pp_ids": [2],       "qty": 15, "due_days": 9,  "user_id": 4, "ptn_loc_id": 3},
    # 납기 여유 (D+16) — ptn_loc_id=1
    {"prod_id": 4, "pp_ids": [],        "qty": 8,  "due_days": 16, "user_id": 5, "ptn_loc_id": 1},
]

_MFG_SCENARIOS = [
    # 이미 생산 중인 주문 (UI 어디에도 표시 안 됨 — realistic 배경 데이터)
    {"prod_id": 3, "pp_ids": [4],    "qty": 25, "due_days": 3, "user_id": 4, "ptn_loc_id": 2},
    {"prod_id": 5, "pp_ids": [1],    "qty": 12, "due_days": 7, "user_id": 5, "ptn_loc_id": 3},
]

_ADMIN_USER_ID    = 1
_OPERATOR_USER_ID = 2
_SHIP_ADDR        = "서울특별시 강남구 테헤란로 123"

_MOCK_TAG = "MOCK09"   # ord_log.note 등 없으므로 cleanup 시 ord_id 범위로 처리


# ─── DB 헬퍼 ─────────────────────────────────────────────────────────────────

def _validate_user(cur, user_id: int, expected_role: str) -> dict:
    cur.execute(
        "SELECT user_id, user_nm, co_nm, role FROM user_account WHERE user_id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    if not row or row["role"] != expected_role:
        actual = row["role"] if row else "not found"
        raise ValueError(f"user_id {user_id} must be {expected_role}; actual={actual}")
    return row


def _load_product_context(cur, prod_id: int, pp_ids: list[int]) -> dict:
    cur.execute(
        "SELECT prod_id, cate_cd, base_price FROM product WHERE prod_id = %s",
        (prod_id,),
    )
    product = cur.fetchone()
    if not product:
        raise ValueError(f"prod_id {prod_id} not found")

    cur.execute(
        """
        SELECT prod_opt_id, diameter, thickness, material, load_class
          FROM product_option
         WHERE prod_id = %s
         ORDER BY prod_opt_id
         LIMIT 1
        """,
        (prod_id,),
    )
    option = cur.fetchone()
    if not option:
        raise ValueError(f"product_option for prod_id {prod_id} not found")

    valid_pp: list[dict] = []
    for pp_id in sorted(set(pp_ids)):
        cur.execute(
            "SELECT pp_id, pp_nm, extra_cost FROM pp_options WHERE pp_id = %s",
            (pp_id,),
        )
        pp = cur.fetchone()
        if not pp:
            raise ValueError(f"pp_id {pp_id} not found")
        valid_pp.append(pp)

    pp_mask = sum(1 << (int(pp["pp_id"]) - 1) for pp in valid_pp)
    cur.execute(
        """
        SELECT pattern_id, pattern_nm
          FROM product_order_pattern_master
         WHERE prod_id = %s
           AND diameter = %s
           AND thickness = %s
           AND material = %s
           AND load_class = %s
           AND pp_mask = %s
           AND is_active = TRUE
        """,
        (
            prod_id,
            option["diameter"],
            option["thickness"],
            option["material"],
            option["load_class"],
            pp_mask,
        ),
    )
    pattern = cur.fetchone()
    if not pattern:
        raise ValueError(
            f"product_order_pattern_master 미매칭: prod_id={prod_id}, pp_mask={pp_mask}"
        )

    return {"product": product, "option": option, "pp": valid_pp, "pattern": pattern}


def _insert_order(
    *,
    cur,
    user_id: int,
    prod_id: int,
    qty: int,
    pp_ids: list[int],
    due_date: date,
    ship_addr: str,
) -> dict:
    """ord ~ ord_log(RCVD) 까지 삽입. ord_id 포함 context 반환."""
    ctx = _load_product_context(cur, prod_id, pp_ids)
    product = ctx["product"]
    option = ctx["option"]
    valid_pp = ctx["pp"]
    pattern = ctx["pattern"]

    final_price = (
        int(product["base_price"]) + sum(int(pp["extra_cost"]) for pp in valid_pp)
    ) * qty

    cur.execute(
        "INSERT INTO ord (user_id) VALUES (%s) RETURNING ord_id, created_at",
        (user_id,),
    )
    row = cur.fetchone()
    ord_id = row["ord_id"]

    cur.execute(
        """
        INSERT INTO ord_detail
            (ord_id, prod_id, diameter, thickness, material, load_class,
             qty, final_price, due_date, ship_addr)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            ord_id, prod_id,
            option["diameter"], option["thickness"],
            option["material"], option["load_class"],
            qty, final_price, due_date.isoformat(), ship_addr,
        ),
    )

    for pp in valid_pp:
        cur.execute(
            "INSERT INTO ord_pp_map (ord_id, pp_id) VALUES (%s, %s)",
            (ord_id, pp["pp_id"]),
        )

    cur.execute(
        "INSERT INTO ord_pattern (ord_id, pattern_id) VALUES (%s, %s)",
        (ord_id, pattern["pattern_id"]),
    )

    cur.execute("INSERT INTO ord_txn (ord_id, txn_type) VALUES (%s, 'RCVD')", (ord_id,))
    cur.execute(
        "INSERT INTO ord_stat (ord_id, user_id, ord_stat) VALUES (%s, %s, 'RCVD')",
        (ord_id, user_id),
    )
    cur.execute(
        "INSERT INTO ord_log (ord_id, prev_stat, new_stat, changed_by) VALUES (%s, NULL, 'RCVD', %s)",
        (ord_id, user_id),
    )

    return {
        "ord_id": ord_id,
        "prod_id": prod_id,
        "cate_cd": product["cate_cd"],
        "qty": qty,
        "final_price": final_price,
        "due_date": due_date,
        "pp": valid_pp,
        "pattern": pattern,
    }


def _approve(cur, ord_id: int, admin_id: int) -> None:
    cur.execute("INSERT INTO ord_txn (ord_id, txn_type) VALUES (%s, 'APPR')", (ord_id,))
    cur.execute(
        "UPDATE ord_stat SET ord_stat='APPR', user_id=%s, updated_at=now() WHERE ord_id=%s",
        (admin_id, ord_id),
    )
    cur.execute(
        "INSERT INTO ord_log (ord_id, prev_stat, new_stat, changed_by) VALUES (%s, 'RCVD', 'APPR', %s)",
        (ord_id, admin_id),
    )


def _set_pattern_location(cur, ord_id: int, ptn_loc_id: int) -> None:
    cur.execute(
        "UPDATE ord_pattern SET ptn_loc_id=%s, assigned_at=now() WHERE ord_id=%s",
        (ptn_loc_id, ord_id),
    )


def _start_mfg(cur, ord_id: int, operator_id: int) -> None:
    # ord_txn 은 RCVD/APPR/CNCL/REJT 만 허용 — MFG 는 ord_stat + ord_log 만 갱신
    cur.execute(
        "UPDATE ord_stat SET ord_stat='MFG', user_id=%s, updated_at=now() WHERE ord_id=%s",
        (operator_id, ord_id),
    )
    cur.execute(
        "INSERT INTO ord_log (ord_id, prev_stat, new_stat, changed_by) VALUES (%s, 'APPR', 'MFG', %s)",
        (ord_id, operator_id),
    )


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PyQt UI 통합 테스트용 종합 Mock 데이터 생성",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="이 스크립트가 생성한 주문(ord_log 마지막 항목 기준)을 삭제하고 재생성",
    )
    p.add_argument("--admin-id",    type=int, default=_ADMIN_USER_ID)
    p.add_argument("--operator-id", type=int, default=_OPERATOR_USER_ID)
    p.add_argument("--today",       type=str, default="",
                   help="기준일 YYYY-MM-DD (기본: 오늘)")
    return p.parse_args()


def _clean_previous(cur) -> int:
    """item 이 없는 RCVD/APPR/MFG 주문을 자식 테이블부터 순서대로 삭제."""
    cur.execute(
        """
        SELECT o.ord_id FROM ord o
          JOIN ord_stat os ON os.ord_id = o.ord_id
        WHERE os.ord_stat IN ('RCVD', 'APPR', 'MFG')
          AND NOT EXISTS (SELECT 1 FROM item WHERE item.ord_id = o.ord_id)
        """
    )
    rows = cur.fetchall()
    if not rows:
        return 0
    ids = [r["ord_id"] for r in rows]
    # FK 순서대로 자식 테이블 먼저 삭제
    cur.execute("DELETE FROM ord_log     WHERE ord_id = ANY(%s)", (ids,))
    cur.execute("DELETE FROM ord_txn     WHERE ord_id = ANY(%s)", (ids,))
    cur.execute("DELETE FROM ord_stat    WHERE ord_id = ANY(%s)", (ids,))
    cur.execute("DELETE FROM ord_pattern WHERE ord_id = ANY(%s)", (ids,))
    cur.execute("DELETE FROM ord_pp_map  WHERE ord_id = ANY(%s)", (ids,))
    cur.execute("DELETE FROM ord_detail  WHERE ord_id = ANY(%s)", (ids,))
    cur.execute("DELETE FROM ord         WHERE ord_id = ANY(%s)", (ids,))
    return len(ids)


def main() -> int:
    _db.load_env()
    args = parse_args()

    today = date.today()
    if args.today:
        try:
            today = date.fromisoformat(args.today[:10])
        except ValueError:
            print(f"ERROR: invalid --today: {args.today}", file=sys.stderr)
            return 1

    created_unreg: list[dict] = []
    created_reg: list[dict] = []
    created_mfg: list[dict] = []

    try:
        with _db.connect() as conn:
            cur = conn.cursor()
            admin    = _validate_user(cur, args.admin_id,    "admin")
            operator = _validate_user(cur, args.operator_id, "operator")

            if args.clean:
                deleted = _clean_previous(cur)
                print(f"  [clean] 기존 테스트 주문 {deleted}건 삭제 완료\n")

            # ── APPR + ptn_loc_id 없음 ──────────────────────────────────────
            for s in _UNREG_SCENARIOS:
                due = today + timedelta(days=s["due_days"])
                ctx = _insert_order(
                    cur=cur,
                    user_id=s["user_id"],
                    prod_id=s["prod_id"],
                    qty=s["qty"],
                    pp_ids=s["pp_ids"],
                    due_date=due,
                    ship_addr=_SHIP_ADDR,
                )
                _approve(cur, ctx["ord_id"], args.admin_id)
                created_unreg.append({**ctx, "ptn_loc_id": None})

            # ── APPR + ptn_loc_id 있음 ──────────────────────────────────────
            for s in _REG_SCENARIOS:
                due = today + timedelta(days=s["due_days"])
                ctx = _insert_order(
                    cur=cur,
                    user_id=s["user_id"],
                    prod_id=s["prod_id"],
                    qty=s["qty"],
                    pp_ids=s["pp_ids"],
                    due_date=due,
                    ship_addr=_SHIP_ADDR,
                )
                _approve(cur, ctx["ord_id"], args.admin_id)
                _set_pattern_location(cur, ctx["ord_id"], s["ptn_loc_id"])
                created_reg.append({**ctx, "ptn_loc_id": s["ptn_loc_id"]})

            # ── MFG 상태 ────────────────────────────────────────────────────
            for s in _MFG_SCENARIOS:
                due = today + timedelta(days=s["due_days"])
                ctx = _insert_order(
                    cur=cur,
                    user_id=s["user_id"],
                    prod_id=s["prod_id"],
                    qty=s["qty"],
                    pp_ids=s["pp_ids"],
                    due_date=due,
                    ship_addr=_SHIP_ADDR,
                )
                _approve(cur, ctx["ord_id"], args.admin_id)
                _set_pattern_location(cur, ctx["ord_id"], s["ptn_loc_id"])
                _start_mfg(cur, ctx["ord_id"], args.operator_id)
                created_mfg.append({**ctx, "ptn_loc_id": s["ptn_loc_id"]})

            cur.close()
        # auto-commit

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # ── 결과 출력 ────────────────────────────────────────────────────────────
    print("=" * 72)
    print("PyQt Mock 데이터 생성 완료")
    print("=" * 72)
    print(f"  기준일: {today}  admin={admin['user_nm']}  operator={operator['user_nm']}")
    print()

    print("【 패턴 미등록 (→ _unreg_list) 】")
    for r in created_unreg:
        pp_label = ",".join(str(pp["pp_id"]) for pp in r["pp"]) or "없음"
        print(
            f"  ord_id={r['ord_id']:<4} APPR  {r['cate_cd']} "
            f"qty={r['qty']:<3} pp=[{pp_label}]  due={r['due_date']}  ptn_loc=없음"
        )

    print()
    print("【 패턴 등록완료 (→ _reg_list, 우선순위 계산 가능) 】")
    for r in created_reg:
        pp_label = ",".join(str(pp["pp_id"]) for pp in r["pp"]) or "없음"
        print(
            f"  ord_id={r['ord_id']:<4} APPR  {r['cate_cd']} "
            f"qty={r['qty']:<3} pp=[{pp_label}]  due={r['due_date']}  ptn_loc={r['ptn_loc_id']}"
        )

    print()
    print("【 MFG 생산 중 (UI 미표시) 】")
    for r in created_mfg:
        pp_label = ",".join(str(pp["pp_id"]) for pp in r["pp"]) or "없음"
        print(
            f"  ord_id={r['ord_id']:<4} MFG   {r['cate_cd']} "
            f"qty={r['qty']:<3} pp=[{pp_label}]  due={r['due_date']}  ptn_loc={r['ptn_loc_id']}"
        )

    print()
    print("확인 방법:")
    print("  1. ./scripts/run-backend.sh  (Management gRPC :50051 필요)")
    print("  2. ./scripts/run-pyqt.sh")
    print("  3. 첫 탭 '패턴 위치 조작 및 생산 시작' 확인:")
    print(f"       패턴 미등록 영역: {len(created_unreg)}건")
    print(f"       생산 계획 영역:   {len(created_reg)}건  (전체 선택 후 우선순위 계산 클릭)")
    print()
    print("재생성 시: python scripts/09_seed_mock_data.py --clean")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
