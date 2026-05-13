"""E2E inspection flow watcher — HW 모드는 RDS 폴링, SIM 모드는 인-프로세스 사슬 직접 호출.

HW 모드 (--mode hw):
    1. 기준 insp_task_txn.max(txn_id) 스냅샷
    2. 빨간 푸시 버튼 push → ESP32 STOPPED → Jetson 캡처 + UploadInspectionImage →
       backend 사슬 → insp_task_txn PROC 신규 INSERT → ai_inference 실행 → SUCC/FAIL
    3. 폴링 (1 sec) 로 PROC 신규 발견 → SUCC/FAIL 까지 timeline 출력
    4. 최종 4-table 일관성 검증

SIM 모드 (--mode sim):
    1. PROC insp_task_txn 직접 INSERT (Tof2 진입 시뮬레이션)
    2. 더미 JPEG 디스크 저장 → AIAdapter.execute 직접 호출
    3. DB 결과 SELECT 로 검증

기본적으로 검증 완료 후 test row 정리 (--keep-data 로 보존 가능).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


def _ensure_path() -> None:
    here = Path(__file__).resolve()
    main_service_root = here.parents[2]
    src = main_service_root / "src"
    paths = [
        str(src / "management_service"),
        str(src),
        str(main_service_root.parent),  # server/
    ]
    for p in paths:
        if p not in sys.path:
            sys.path.insert(0, p)


_ensure_path()

# DATABASE_URL 은 .env.local 에서 미리 export 되어 있어야 함.
if not os.environ.get("DATABASE_URL"):
    print("✗ DATABASE_URL 미설정 — .env.local 자동 로드 후 재실행하세요.")
    sys.exit(2)
os.environ.setdefault("SMARTCAST_SCHEMA", "smartcast")

from smart_cast_db.database import SessionLocal  # noqa: E402
from smart_cast_db.models import (  # noqa: E402
    AiInferenceTxn,
    AiModel,
    InspStat,
    InspTaskTxn,
    Item,
)


@dataclass
class FlowSnapshot:
    txn_id: int
    item_id: int | None
    txn_stat: str
    result: bool | None
    start_at: datetime | None
    end_at: datetime | None
    inference_id: int | None
    inference_stat: str | None
    predicted_class: str | None
    yolo_confidence: float | None
    final_result: str | None
    item_is_defective: bool | None


def _snapshot_flow(db, txn_id: int) -> FlowSnapshot:
    insp = db.get(InspTaskTxn, txn_id)
    if insp is None:
        return FlowSnapshot(
            txn_id=txn_id,
            item_id=None,
            txn_stat="<gone>",
            result=None,
            start_at=None,
            end_at=None,
            inference_id=None,
            inference_stat=None,
            predicted_class=None,
            yolo_confidence=None,
            final_result=None,
            item_is_defective=None,
        )
    inference = (
        db.query(AiInferenceTxn)
        .filter(AiInferenceTxn.insp_txn_id == txn_id)
        .order_by(AiInferenceTxn.inference_id.desc())
        .first()
    )
    stat = db.get(InspStat, txn_id)
    item = db.get(Item, insp.item_id) if insp.item_id else None
    return FlowSnapshot(
        txn_id=insp.txn_id,
        item_id=insp.item_id,
        txn_stat=insp.txn_stat or "",
        result=insp.result,
        start_at=insp.start_at,
        end_at=insp.end_at,
        inference_id=inference.inference_id if inference else None,
        inference_stat=inference.txn_stat if inference else None,
        predicted_class=stat.predicted_class if stat else None,
        yolo_confidence=float(stat.yolo_confidence) if stat and stat.yolo_confidence is not None else None,
        final_result=stat.final_result if stat else None,
        item_is_defective=item.is_defective if item else None,
    )


def _max_txn_id(db) -> int:
    row = db.query(InspTaskTxn.txn_id).order_by(InspTaskTxn.txn_id.desc()).first()
    return int(row[0]) if row else 0


def _print_timeline(label: str, snap: FlowSnapshot) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(
        f"  [{ts}] {label:14s} "
        f"txn={snap.txn_id} item={snap.item_id} "
        f"stat={snap.txn_stat:5s} result={snap.result!s:5s} "
        f"inference={snap.inference_id or '-':>3} ({snap.inference_stat or '-':4s}) "
        f"class={snap.predicted_class or '-':4s} final={snap.final_result or '-':2s} "
        f"item.is_defective={snap.item_is_defective!s}"
    )


def _validate_succ(snap: FlowSnapshot) -> tuple[bool, list[str]]:
    fails: list[str] = []
    if snap.txn_stat != "SUCC":
        fails.append(f"insp_task_txn.txn_stat={snap.txn_stat} (expected SUCC)")
    if snap.result is None:
        fails.append("insp_task_txn.result IS NULL (expected True/False)")
    if snap.end_at is None:
        fails.append("insp_task_txn.end_at IS NULL")
    if snap.inference_id is None:
        fails.append("ai_inference_txn 미생성")
    elif snap.inference_stat != "SUCC":
        fails.append(f"ai_inference_txn.txn_stat={snap.inference_stat} (expected SUCC)")
    if snap.final_result not in ("GP", "DP"):
        fails.append(f"insp_stat.final_result={snap.final_result} (expected GP|DP)")
    if snap.predicted_class not in ("CMH", "RMH", "EMH"):
        fails.append(f"insp_stat.predicted_class={snap.predicted_class}")
    expected_defective = (snap.final_result == "DP")
    if snap.item_is_defective != expected_defective:
        fails.append(
            f"item.is_defective={snap.item_is_defective} (expected {expected_defective})"
        )
    return (len(fails) == 0, fails)


def _validate_fail(snap: FlowSnapshot) -> tuple[bool, list[str]]:
    fails: list[str] = []
    if snap.txn_stat != "FAIL":
        fails.append(f"insp_task_txn.txn_stat={snap.txn_stat} (expected FAIL)")
    if snap.end_at is None:
        fails.append("insp_task_txn.end_at IS NULL")
    if snap.inference_id is not None:
        fails.append("ai_inference_txn 가 생성됨 (실패 path 인데 비정상)")
    return (len(fails) == 0, fails)


# ---------- HW mode -----------------------------------------------------------
def run_hw_mode(timeout_sec: int, keep_data: bool) -> int:
    with SessionLocal() as db:
        baseline = _max_txn_id(db)
    print(f"  baseline insp_task_txn.max(txn_id) = {baseline}")
    print(f"  최대 {timeout_sec}초 동안 신규 PROC → SUCC/FAIL 을 감시합니다.\n")

    deadline = time.time() + timeout_sec
    seen_proc: set[int] = set()
    completed: Optional[FlowSnapshot] = None

    while time.time() < deadline and completed is None:
        time.sleep(1.0)
        with SessionLocal() as db:
            rows = (
                db.query(InspTaskTxn)
                .filter(InspTaskTxn.txn_id > baseline)
                .order_by(InspTaskTxn.txn_id.asc())
                .all()
            )
            for row in rows:
                if row.txn_id not in seen_proc and row.txn_stat == "PROC":
                    seen_proc.add(row.txn_id)
                    snap = _snapshot_flow(db, row.txn_id)
                    _print_timeline("PROC 감지", snap)
                if row.txn_stat in ("SUCC", "FAIL"):
                    snap = _snapshot_flow(db, row.txn_id)
                    _print_timeline(f"{row.txn_stat} 도달", snap)
                    completed = snap
                    break

    if completed is None:
        print(f"\n  ✗ 시간 초과 — {timeout_sec}초 안에 신규 검사 사이클이 감지되지 않았습니다.")
        print("    체크리스트:")
        print("    · ESP32 펌웨어 v1.7.0 USB Serial 연결 OK?")
        print("    · Jetson casting-image-publisher systemd 활성?")
        print("    · backend EventGateway servicer 로그에 HANDOFF_ACK 도달?")
        print("    · backend UploadInspectionImage RPC handler 가 ai_adapter 호출?")
        return 1

    print()
    if completed.txn_stat == "SUCC":
        ok, errors = _validate_succ(completed)
    else:
        ok, errors = _validate_fail(completed)
    return _finalize(completed, ok, errors, keep_data, created_txn_id=None)


# ---------- SIM mode ----------------------------------------------------------
def run_sim_mode(timeout_sec: int, keep_data: bool) -> int:
    from sqlalchemy import text

    from services.command.ai_inference_command import AiInferenceCommand
    from services.command.inspection_image_sink_command import InspectionImageSinkCommand
    from services.command.inspection_result_command import InspectionResultCommand
    from services.core.adapters.ai_adapter import AIAdapter

    # 1. 가용 item 확보 — 없으면 임시 item INSERT (FK: ord 한 행 필요)
    temp_item_id: int | None = None
    with SessionLocal() as db:
        item = (
            db.query(Item)
            .filter(Item.is_defective.is_(None))
            .order_by(Item.item_id.desc())
            .first()
        )
        if item is None:
            ord_row = db.execute(
                text("SELECT ord_id FROM smartcast.ord ORDER BY ord_id ASC LIMIT 1")
            ).fetchone()
            if ord_row is None:
                print("✗ smartcast.ord 행이 없음 — temp item 생성 불가")
                return 1
            ord_id = int(ord_row[0])
            new_item = Item(ord_id=ord_id, cur_stat="CAST", is_defective=None)
            db.add(new_item)
            db.commit()
            db.refresh(new_item)
            item_id = int(new_item.item_id)
            temp_item_id = item_id
            print(f"  [SIM] temp item INSERT — item_id={item_id} ord_id={ord_id} (cleanup 시 DELETE)")
        else:
            item_id = int(item.item_id)
            print(f"  [SIM] 기존 item 사용 — item_id={item_id}")
        yolo = (
            db.query(AiModel)
            .filter(AiModel.is_active.is_(True))
            .filter(AiModel.model_type == "YOLO")
            .first()
        )
        if yolo is None:
            print("✗ active YOLO ai_model 없음")
            return 1

    # 2. PROC insp_task_txn 직접 INSERT (Tof2 진입 시뮬레이션)
    with SessionLocal() as db:
        row = InspTaskTxn(item_id=item_id, txn_stat="PROC")
        db.add(row)
        db.commit()
        db.refresh(row)
        created_txn_id = int(row.txn_id)
    print(f"  [SIM] PROC insp_task_txn INSERT — txn_id={created_txn_id} item_id={item_id}")

    # 3. 더미 JPEG 디스크 저장 (UploadInspectionImage RPC 시뮬레이션)
    sink = InspectionImageSinkCommand()
    saved = sink.save(
        item_id=item_id,
        image_bytes=b"\xff\xd8\xff\xe0" + b"sim-e2e-jpeg" * 16 + b"\xff\xd9",
        label="e2e_sim",
    )
    if saved is None:
        print("✗ InspectionImageSinkCommand.save 실패")
        return 1
    print(f"  [SIM] 검사 이미지 저장 — {saved.path}")

    # 4. AIAdapter 실 호출 (mock AI 서버) — 추론 결과만 반환, DB 갱신은 별도 단계
    # P2.4 (2026-05-13): AIAdapter 가 InspectionResultCommand 를 직접 호출하지 않으므로
    # 본 e2e 시뮬레이션도 추론 결과를 직접 받아서 DB 영속화를 별도로 트리거한다.
    adapter = AIAdapter(ai_command=AiInferenceCommand())
    payload = json.dumps(
        {
            "image_path": str(saved.path),
            "captured_at": time.time(),
            "label": "e2e_sim",
        }
    ).encode("utf-8")
    started = time.time()
    result = adapter.execute(
        item_id=item_id,
        _robot_id="AI",
        _command="AI_INFERENCE_REQUEST",
        payload=payload,
    )
    elapsed_ms = (time.time() - started) * 1000.0
    print(f"  [SIM] AIAdapter.execute → success={result.success} ({elapsed_ms:.1f}ms)")

    # 4b. DB 4-table 영속화 — 실 HW 흐름에서는 task_executor 가
    # state_manager.record_inspection_result 를 호출. SIM 모드에서는 직접 호출.
    inference_dict = (result.payload or {}).get("inference") or {}
    result_cmd = InspectionResultCommand()
    if inference_dict.get("ok"):
        from datetime import datetime as _dt
        def _to_dt(value):
            if not value:
                return None
            if isinstance(value, _dt):
                return value
            try:
                return _dt.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                return None
        try:
            result_cmd.record_inspection_result(
                item_id=item_id,
                is_defective=bool(inference_dict.get("is_defective")),
                predicted_class=inference_dict.get("predicted_class"),
                yolo_confidence=inference_dict.get("yolo_confidence"),
                anomaly_score=inference_dict.get("anomaly_score"),
                anomaly_threshold=inference_dict.get("anomaly_threshold"),
                model_id=inference_dict.get("model_id"),
                model_type=inference_dict.get("model_type"),
                step_type=inference_dict.get("step_type"),
                started_at=_to_dt(inference_dict.get("started_at")),
                completed_at=_to_dt(inference_dict.get("completed_at")),
                raw_inference_payload=inference_dict.get("raw_payload"),
            )
            print("  [SIM] InspectionResultCommand.record_inspection_result OK")
        except (LookupError, ValueError) as exc:
            print(f"  [SIM] record_inspection_result 예외 → record_inspection_failure: {exc}")
            result_cmd.record_inspection_failure(item_id=item_id, reason=str(exc))
    else:
        reason = inference_dict.get("error_reason") or "ai_inference_failed"
        result_cmd.record_inspection_failure(item_id=item_id, reason=reason)
        print(f"  [SIM] InspectionResultCommand.record_inspection_failure reason={reason}")

    # 5. ToPAWait/CONV_ALLOW_MOVE 시뮬레이션 — conv_adapter 가 command_queue 로
    # ConveyorCmd("RUN") enqueue (WatchConveyorCommands → Jetson Serial relay).
    # 실 HW 흐름에서는 AMR 이 ToINSP pose 도착 후 ToPAWait step 3 가 conv_adapter 호출.
    from services.core.adapters.conv_adapter import ConvAdapter
    from services.legacy.command_queue import queue as command_queue

    conv = ConvAdapter()
    conv_ok, conv_msg = conv.execute(
        item_id=item_id,
        robot_id="CONV1",
        command="CONV_ALLOW_MOVE",
        payload=json.dumps({"duration_sec": 4.0}).encode("utf-8"),
    )
    print(f"  [SIM] ConvAdapter.execute(CONV_ALLOW_MOVE) → ok={conv_ok} msg={conv_msg}")
    cmd = command_queue.wait_next(robot_id_filter=None, timeout=1.0)
    if cmd is not None:
        print(
            f"  [SIM] ConveyorCmd 수신 — robot_id={cmd.robot_id} command={cmd.command} "
            f"item_id={cmd.item_id} issued_by={cmd.issued_by}"
        )
    else:
        print("  ⚠ ConveyorCmd 미수신 (conv_adapter enqueue 실패?)")

    # 6. DB 4-table 검증
    with SessionLocal() as db:
        snap = _snapshot_flow(db, created_txn_id)
    _print_timeline("SIM 완료", snap)

    if result.success:
        ok, errors = _validate_succ(snap)
    else:
        ok, errors = _validate_fail(snap)
    return _finalize(
        snap, ok, errors, keep_data,
        created_txn_id=created_txn_id, temp_item_id=temp_item_id,
    )


# ---------- finalize / cleanup ------------------------------------------------
def _finalize(
    snap: FlowSnapshot,
    ok: bool,
    errors: list[str],
    keep_data: bool,
    created_txn_id: int | None,
    temp_item_id: int | None = None,
) -> int:
    print("\n  ──────────── 검증 ────────────")
    if ok:
        print("  ✅ 4-table 일관성 검증 통과")
        print(f"     insp_task_txn:     txn={snap.txn_id} stat={snap.txn_stat} result={snap.result}")
        print(f"     ai_inference_txn:  inference_id={snap.inference_id} stat={snap.inference_stat}")
        print(f"     insp_stat:         class={snap.predicted_class} conf={snap.yolo_confidence} final={snap.final_result}")
        print(f"     item.is_defective: {snap.item_is_defective}")
    else:
        print("  ❌ 검증 실패:")
        for err in errors:
            print(f"     · {err}")

    if not keep_data:
        # SIM 모드에서만 cleanup (HW 모드는 실 데이터를 보존)
        if created_txn_id is not None:
            _cleanup(created_txn_id, snap.item_id, temp_item_id)
    else:
        print("\n  --keep-data 지정 — 테스트 row 보존 (수동 정리 필요)")

    return 0 if ok else 1


def _cleanup(txn_id: int, item_id: int | None, temp_item_id: int | None = None) -> None:
    print(f"\n  [cleanup] insp_txn={txn_id} item={item_id} temp_item={temp_item_id} 정리...")
    with SessionLocal() as db:
        db.query(InspStat).filter(InspStat.insp_txn_id == txn_id).delete(synchronize_session=False)
        db.flush()
        db.query(AiInferenceTxn).filter(AiInferenceTxn.insp_txn_id == txn_id).delete(
            synchronize_session=False
        )
        db.flush()
        db.query(InspTaskTxn).filter(InspTaskTxn.txn_id == txn_id).delete(synchronize_session=False)
        db.flush()
        if temp_item_id is not None:
            # SIM 이 임시 생성한 item — DELETE
            db.query(Item).filter(Item.item_id == temp_item_id).delete(synchronize_session=False)
        elif item_id is not None:
            # 기존 item — is_defective 만 NULL 복원
            db.query(Item).filter(Item.item_id == item_id).update(
                {Item.is_defective: None},
                synchronize_session=False,
            )
        db.commit()
    print(f"  [cleanup] 완료")


# ---------- entry -------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="E2E inspection flow watcher")
    parser.add_argument("--mode", choices=["hw", "sim"], default="hw")
    parser.add_argument("--timeout", type=int, default=180, help="HW 모드 대기 timeout (sec)")
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="검증 후 테스트 row 보존 (기본 cleanup)",
    )
    args = parser.parse_args()

    print(f"  mode={args.mode}  timeout={args.timeout}s  keep_data={args.keep_data}")
    print()

    if args.mode == "hw":
        return run_hw_mode(timeout_sec=args.timeout, keep_data=args.keep_data)
    return run_sim_mode(timeout_sec=args.timeout, keep_data=args.keep_data)


if __name__ == "__main__":
    raise SystemExit(main())
