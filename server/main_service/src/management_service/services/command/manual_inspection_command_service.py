"""작업자가 확정한 검사 결과 처리."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from smart_cast_db.database import SessionLocal
from smart_cast_db.models import InspStat, InspTaskTxn, Item
from sqlalchemy.orm import Session

from services.contracts.enums import EventType
from services.contracts.models import Event


@dataclass(frozen=True)
class CompleteInspectionResult:
    """검사 완료 응답과 후속 이벤트에 필요한 값."""

    txn_id: int
    item_id: int | None
    txn_stat: str
    result: str
    req_at: datetime | None
    start_at: datetime | None
    end_at: datetime


def complete_inspection(
    db: Session,
    *,
    txn_id: int,
    result: bool,
    completed_at: datetime | None = None,
) -> CompleteInspectionResult:
    """검사 작업, 검사 상태, 제품 판정을 같은 트랜잭션에 반영.

    호출자가 commit을 결정한다.

    Raises:
        LookupError: 대상 검사 작업이 없을 때
    """
    txn = db.get(InspTaskTxn, txn_id)
    if txn is None:
        raise LookupError(f"insp_task_txn={txn_id} not found")

    completed_at = completed_at or datetime.utcnow()
    txn.txn_stat = "SUCC"
    txn.result = result
    txn.end_at = completed_at

    stat = db.get(InspStat, txn_id)
    if stat is None:
        stat = InspStat(insp_txn_id=txn_id, item_id=txn.item_id)
        db.add(stat)
    stat.item_id = txn.item_id
    stat.final_result = "GP" if result else "DP"
    stat.updated_at = completed_at

    if txn.item_id is not None:
        item = db.get(Item, txn.item_id)
        if item is not None:
            item.result = result
            item.updated_at = completed_at

    db.flush()
    return CompleteInspectionResult(
        txn_id=txn.txn_id,
        item_id=txn.item_id,
        txn_stat=txn.txn_stat,
        result="OK" if result else "NG",
        req_at=txn.req_at,
        start_at=txn.start_at,
        end_at=txn.end_at,
    )


class ManualInspectionCommandService:
    """검사 DB 변경을 먼저 확정한 뒤 완료 이벤트를 발행."""

    def __init__(
        self,
        event_bridge: Any,
        session_factory: Callable[[], Any] = SessionLocal,
    ) -> None:
        self._event_bridge = event_bridge
        self._session_factory = session_factory

    def complete(self, *, txn_id: int, result: bool) -> CompleteInspectionResult:
        with self._session_factory() as db:
            completed = complete_inspection(
                db,
                txn_id=txn_id,
                result=result,
            )
            db.commit()

        self._event_bridge.publish(
            Event(
                event_type=EventType.INSP_COMPLETED,
                txn_id=completed.txn_id,
                item_id=completed.item_id,
                payload={
                    "result": completed.result,
                    "is_defective": not result,
                    "source": "management.complete_inspection",
                },
            )
        )
        return completed
