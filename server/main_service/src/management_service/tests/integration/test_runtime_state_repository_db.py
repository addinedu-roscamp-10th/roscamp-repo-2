from __future__ import annotations

import asyncio
from decimal import Decimal

from services.contracts.enums import TaskType, TxnStat


def _seed_user(models, db, *, user_id: int = 1) -> None:
    db.add(
        models.UserAccount(
            user_id=user_id,
            co_nm="Test Co",
            user_nm="tester",
            role="admin",
            email=f"tester{user_id}@example.com",
            password="secret",
        )
    )


def _seed_order_bundle(models, db, *, ord_id: int, qty: int, user_id: int = 1) -> None:
    db.add(models.Ord(ord_id=ord_id, user_id=user_id))
    db.add(models.OrdDetail(ord_id=ord_id, qty=qty))
    db.add(models.OrdPattern(ord_id=ord_id))
    db.add(models.OrdStat(ord_id=ord_id, user_id=user_id, ord_stat="APPR"))

def _seed_tat(models, db, *, res_id: str = "TAT1") -> None:
    db.add(models.Res(res_id=res_id, res_type="TAT", model_nm="test-tat"))
    db.add(models.Trans(res_id=res_id, slot_count=1, max_load_kg=Decimal("100.0")))

def test_start_production_async_commits_items_and_order_status(runtime_repo_db) -> None:
    """생산 시작 후 생성 Item과 주문 상태가 비동기 세션에서 함께 반영되는지 검증."""
    models = runtime_repo_db.models
    repo = runtime_repo_db.repo

    with runtime_repo_db.sync_session_factory() as db:
        _seed_user(models, db, user_id=7)
        _seed_order_bundle(models, db, ord_id=101, qty=3, user_id=7)
        db.commit()

    ack = asyncio.run(repo.start_production(101))

    assert ack.accepted is True
    assert ack.ord_id == 101
    assert len(ack.item_ids) == 3

    with runtime_repo_db.sync_session_factory() as db:
        items = db.query(models.Item).filter(models.Item.ord_id == 101).order_by(models.Item.item_id).all()
        stat = db.query(models.OrdStat).filter(models.OrdStat.ord_id == 101).one()
        logs = db.query(models.OrdLog).filter(models.OrdLog.ord_id == 101).all()

        assert [item.cur_stat for item in items] == ["CREATED", "CREATED", "CREATED"]
        assert all(item.cur_res == "PAT1" for item in items)
        assert stat.ord_stat == "MFG"
        assert len(logs) == 1
        assert logs[0].prev_stat == "APPR"
        assert logs[0].new_stat == "MFG"

    second_ack = asyncio.run(repo.start_production(101))

    assert second_ack.accepted is False
    assert "already started on line" in second_ack.reason


def test_reserve_storage_slots_async_is_visible_to_sync_session(runtime_repo_db) -> None:
    """비동기 슬롯 예약 결과가 별도 동기 세션에서도 즉시 조회되는지 검증."""
    models = runtime_repo_db.models
    repo = runtime_repo_db.repo

    # seed_master.sql에 의해 이미 strg_location_stat 테이블에 18개의 'empty' 슬롯이 존재하므로 바로 예약 테스트를 수행합니다.
    reserved_count = asyncio.run(repo.reserve_storage_slots(1, 2, 3))

    assert reserved_count == 3

    with runtime_repo_db.sync_session_factory() as db:
        slots = (
            db.query(models.StrgLocationStat)
            .filter(models.StrgLocationStat.loc_row == 1)
            .order_by(models.StrgLocationStat.loc_col)
            .all()
        )

        # 예약 대상(2, 3, 4)은 'reserved'로 변경되고, 인접 슬롯(1, 5)은 'empty'를 유지하는지 검증합니다.
        assert [(slot.loc_col, slot.status) for slot in slots if slot.loc_col in (1, 2, 3, 4, 5)] == [
            (1, "empty"),
            (2, "reserved"),
            (3, "reserved"),
            (4, "reserved"),
            (5, "empty"),
        ]

    reserved_again = asyncio.run(repo.reserve_storage_slots(1, 2, 3))

    assert reserved_again == 0


def test_increment_order_gp_qty_is_atomic(runtime_repo_db) -> None:
    models = runtime_repo_db.models
    repo = runtime_repo_db.repo

    with runtime_repo_db.sync_session_factory() as db:
        _seed_user(models, db, user_id=8)
        _seed_order_bundle(models, db, ord_id=202, qty=2, user_id=8)
        db.commit()

    async def scenario() -> list[dict]:
        return list(
            await asyncio.gather(
                repo.increment_order_gp_qty(202),
                repo.increment_order_gp_qty(202),
            )
        )

    results = asyncio.run(scenario())

    with runtime_repo_db.sync_session_factory() as db:
        stat = db.get(models.OrdStat, 202)
        assert stat is not None
        assert stat.gp_qty == 2
        assert stat.ord_stat == "DONE"
        done_logs = (
            db.query(models.OrdLog)
            .filter(models.OrdLog.ord_id == 202)
            .filter(models.OrdLog.new_stat == "DONE")
            .all()
        )
        assert len(done_logs) == 1
        assert done_logs[0].prev_stat == "APPR"

    assert sorted(int(result["gp_qty"]) for result in results) == [1, 2]
    assert sum(bool(result["completed"]) for result in results) == 1


def test_sync_task_status_async_sets_lifecycle_timestamps(runtime_repo_db) -> None:
    """비동기 작업 상태 전이에서 자원 배정과 시작·종료 시각 기록을 검증."""
    models = runtime_repo_db.models
    repo = runtime_repo_db.repo

    with runtime_repo_db.sync_session_factory() as db:
        _seed_user(models, db, user_id=9)
        _seed_order_bundle(models, db, ord_id=102, qty=1, user_id=9)
        db.flush()
        item = models.Item(ord_id=102, cur_stat="CREATED", cur_res="PAT1")
        db.add(item)
        db.flush()
        txn = models.TransTaskTxn(
            trans_id=None,
            task_type=TaskType.ToPP.value,
            txn_stat=TxnStat.QUE.value,
            item_id=item.item_id,
            ord_id=102,
        )
        db.add(txn)
        db.commit()
        txn_id = txn.trans_task_txn_id

    asyncio.run(
        repo.sync_task_status(
            {
                "txn_id": txn_id,
                "task_type": TaskType.ToPP.value,
                "status": TxnStat.PROC.value,
                "res_id": "TAT1",
            }
        )
    )
    asyncio.run(
        repo.sync_task_status(
            {
                "txn_id": txn_id,
                "task_type": TaskType.ToPP.value,
                "status": TxnStat.SUCC.value,
                "res_id": "TAT1",
            }
        )
    )

    with runtime_repo_db.sync_session_factory() as db:
        txn = db.get(models.TransTaskTxn, txn_id)

        assert txn is not None
        assert txn.txn_stat == TxnStat.SUCC.value
        assert txn.trans_id == "TAT1"
        assert txn.start_at is not None
        assert txn.end_at is not None

def test_order_completes_only_after_every_toship_task_succeeds(runtime_repo_db) -> None:
    models = runtime_repo_db.models
    repo = runtime_repo_db.repo

    with runtime_repo_db.sync_session_factory() as db:
        _seed_user(models, db, user_id=10)
        _seed_order_bundle(models, db, ord_id=103, qty=2, user_id=10)
        stat = db.query(models.OrdStat).filter(models.OrdStat.ord_id == 103).one()
        stat.ord_stat = "DONE"
        items = [
            models.Item(ord_id=103, cur_stat="READY_TO_SHIP"),
            models.Item(ord_id=103, cur_stat="READY_TO_SHIP"),
            models.Item(ord_id=103, cur_stat="DISCARDED", is_defective=True),
        ]
        db.add_all(items)
        db.flush()
        tasks = [
            models.TransTaskTxn(
                task_type=TaskType.ToSHIP.value,
                txn_stat=TxnStat.SUCC.value,
                item_id=items[0].item_id,
                ord_id=103,
            ),
            models.TransTaskTxn(
                task_type=TaskType.ToSHIP.value,
                txn_stat=TxnStat.PROC.value,
                item_id=items[1].item_id,
                ord_id=103,
            ),
        ]
        db.add_all(tasks)
        db.commit()
        unfinished_task_id = tasks[1].trans_task_txn_id

    assert asyncio.run(repo.mark_order_shipping(103)) is True
    assert asyncio.run(repo.mark_order_shipping(103)) is True
    assert asyncio.run(repo.mark_order_completed_if_shipping_finished(103)) is False

    asyncio.run(
        repo.sync_task_status(
            {
                "txn_id": unfinished_task_id,
                "task_type": TaskType.ToSHIP.value,
                "status": TxnStat.SUCC.value,
            }
        )
    )
    assert asyncio.run(repo.mark_order_completed_if_shipping_finished(103)) is True

    with runtime_repo_db.sync_session_factory() as db:
        stat = db.query(models.OrdStat).filter(models.OrdStat.ord_id == 103).one()
        assert stat.ord_stat == "COMP"
        shipping_logs = (
            db.query(models.OrdLog)
            .filter(models.OrdLog.ord_id == 103)
            .filter(models.OrdLog.new_stat.in_(["SHIP", "COMP"]))
            .order_by(models.OrdLog.log_id)
            .all()
        )
        assert [(log.prev_stat, log.new_stat) for log in shipping_logs] == [
            ("DONE", "SHIP"),
            ("SHIP", "COMP"),
        ]

def test_resource_snapshot_preserves_latest_telemetry(runtime_repo_db) -> None:
    models = runtime_repo_db.models
    repo = runtime_repo_db.repo

    with runtime_repo_db.sync_session_factory() as db:
        _seed_tat(models, db, res_id="TAT9")
        db.commit()

    asyncio.run(
        repo.sync_resource_telemetry(
            {
                "res_id": "TAT9",
                "battery_pct": 67,
            }
        )
    )

    asyncio.run(
        repo.sync_resource_snapshot(
            {
                "res_id": "TAT9",
                "status": "ALLOC",
                "item_id": None,
                "battery_pct": 41,
            }
        )
    )

    with runtime_repo_db.sync_session_factory() as db:
        stat = db.get(models.TransStat, "TAT9")
        assert stat is not None
        assert stat.cur_stat == "ALLOC"
        assert stat.item_id is None
        assert stat.battery_pct == 67
