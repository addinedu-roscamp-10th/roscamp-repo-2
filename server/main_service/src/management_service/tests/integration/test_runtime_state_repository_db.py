from __future__ import annotations

import asyncio

from services.contracts.enums import TaskType, TxnStat


def _seed_order(models, db, *, ord_id: int, qty: int, user_id: int) -> None:
    db.add(models.UserAccount(user_id=user_id, co_nm="Test", user_nm="tester", role="admin", email=f"{user_id}@example.com", password="secret"))
    db.add(models.Ord(ord_id=ord_id, user_id=user_id))
    db.add(models.OrdDetail(ord_id=ord_id, qty=qty))
    db.add(models.OrdPattern(ord_id=ord_id))
    db.add(models.OrdStat(ord_id=ord_id, user_id=user_id, ord_stat="APPR"))


def test_start_production_async_commits_items_and_order_status(runtime_repo_db) -> None:
    """생산 시작 후 생성 Item과 주문 상태가 비동기 세션에서 함께 반영되는지 검증."""
    models, repo = runtime_repo_db.models, runtime_repo_db.repo
    with runtime_repo_db.sync_session_factory() as db:
        _seed_order(models, db, ord_id=101, qty=3, user_id=7)
        db.commit()
    ack = asyncio.run(repo.start_production(101))
    assert ack.accepted is True
    assert len(ack.item_ids) == 3
    with runtime_repo_db.sync_session_factory() as db:
        assert db.query(models.Item).filter(models.Item.ord_id == 101).count() == 3
        assert db.query(models.OrdStat).filter(models.OrdStat.ord_id == 101).one().ord_stat == "MFG"
    assert asyncio.run(repo.start_production(101)).accepted is False


def test_reserve_storage_slots_async_is_visible_to_sync_session(runtime_repo_db) -> None:
    """비동기 슬롯 예약 결과가 별도 동기 세션에서도 즉시 조회되는지 검증."""
    models, repo = runtime_repo_db.models, runtime_repo_db.repo
    assert asyncio.run(repo.reserve_storage_slots(1, 2, 3)) == 3
    with runtime_repo_db.sync_session_factory() as db:
        slots = db.query(models.StrgLocationStat).filter(models.StrgLocationStat.loc_row == 1).order_by(models.StrgLocationStat.loc_col).all()
        assert [(slot.loc_col, slot.status) for slot in slots if slot.loc_col in (1, 2, 3, 4, 5)] == [(1, "empty"), (2, "reserved"), (3, "reserved"), (4, "reserved"), (5, "empty")]
    assert asyncio.run(repo.reserve_storage_slots(1, 2, 3)) == 0


def test_sync_task_status_async_sets_lifecycle_timestamps(runtime_repo_db) -> None:
    """비동기 작업 상태 전이에서 자원 배정과 시작·종료 시각 기록을 검증."""
    models, repo = runtime_repo_db.models, runtime_repo_db.repo
    with runtime_repo_db.sync_session_factory() as db:
        _seed_order(models, db, ord_id=102, qty=1, user_id=9)
        item = models.Item(ord_id=102, cur_stat="CREATED", cur_res="PAT1")
        db.add(item)
        db.flush()
        txn = models.TransTaskTxn(trans_id=None, task_type=TaskType.ToPP.value, txn_stat=TxnStat.QUE.value, item_id=item.item_id, ord_id=102)
        db.add(txn)
        db.commit()
        txn_id = txn.trans_task_txn_id
    for status in (TxnStat.PROC, TxnStat.SUCC):
        asyncio.run(repo.sync_task_status({"txn_id": txn_id, "task_type": TaskType.ToPP.value, "status": status.value, "res_id": "TAT1"}))
    with runtime_repo_db.sync_session_factory() as db:
        txn = db.get(models.TransTaskTxn, txn_id)
        assert txn.txn_stat == TxnStat.SUCC.value
        assert txn.trans_id == "TAT1"
        assert txn.start_at is not None and txn.end_at is not None
