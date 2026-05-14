"""DB write-through repository for Management runtime state.

This module keeps SQLAlchemy and schema mapping details out of StateManager.
The state manager remains the fast in-memory source for orchestration decisions,
while this repository mirrors durable runtime snapshots into smart_cast_db.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any

from sqlalchemy import tuple_

from services.contracts.enums import TaskType, TxnStat
from services.contracts.models import StartProductionOrderAckModel

logger = logging.getLogger(__name__)

TRANS_TASK_TYPES = {
    TaskType.ToPP.value,
    TaskType.ToSTRG.value,
    TaskType.ToSHIP.value,
    TaskType.ToCHG.value,
}
EQUIP_TASK_TYPE_ALIASES = {
    TaskType.ToPAWait.value: "ToPAWait",
}


def _task_db_value(task_type: Any) -> str | None:
    if isinstance(task_type, TaskType):
        value = task_type.value
    elif isinstance(task_type, str):
        value = task_type
    else:
        return None
    return EQUIP_TASK_TYPE_ALIASES.get(value, value)


def _txn_stat_value(status: Any) -> str:
    if isinstance(status, TxnStat):
        return status.value
    value = str(status or TxnStat.QUE.value)
    if value in {stat.value for stat in TxnStat}:
        return value
    upper = value.upper()
    return upper if upper in {stat.value for stat in TxnStat} else TxnStat.QUE.value


def _task_id_to_int(task_id: Any) -> int | None:
    if task_id is None:
        return None
    raw = str(task_id)
    if raw.startswith("task_"):
        raw = raw.removeprefix("task_")
    try:
        return int(raw)
    except ValueError:
        return None


def _normalize_item_id(item_id: Any) -> int | None:
    if item_id is None:
        return None
    try:
        normalized = int(item_id)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _res_kind(res_id: Any) -> str | None:
    if not isinstance(res_id, str) or not res_id:
        return None
    if res_id.startswith("TAT"):
        return "trans"
    return "equip"


_EQUIP_CUR_STAT_VALUES = {
    "IDLE", "ALLOC", "FAIL",
    "MV_SRC", "GRASP", "MV_DEST", "RELEASE", "TO_IDLE",
    "ON", "OFF",
}

_TRANS_CUR_STAT_VALUES = {
    "IDLE", "ALLOC", "CHG", "TO_IDLE",
    "MV_SRC", "WAIT_LD", "MV_DEST", "WAIT_DLD",
    "SUCC", "FAIL",
}


def _equip_stat_value(status: Any) -> str:
    # state_manager가 DDL cur_stat 값을 직접 넘겨주면 그대로 사용
    if isinstance(status, str) and status in _EQUIP_CUR_STAT_VALUES:
        return status
    # 그렇지 않으면 TxnStat 기반 폴백 매핑
    value = _txn_stat_value(status)
    if value == TxnStat.FAIL.value:
        return "FAIL"
    if value == TxnStat.QUE.value:
        return "ALLOC"
    if value == TxnStat.PROC.value:
        return "ON"
    return "IDLE"


def _trans_stat_value(status: Any) -> str:
    # state_manager가 DDL cur_stat 값을 직접 넘겨주면 그대로 사용
    if isinstance(status, str) and status in _TRANS_CUR_STAT_VALUES:
        return status
    # 그렇지 않으면 TxnStat 기반 폴백 매핑
    value = _txn_stat_value(status)
    if value == TxnStat.QUE.value:
        return "ALLOC"
    if value == TxnStat.PROC.value:
        return "MV_SRC"
    if value in {TxnStat.SUCC.value, TxnStat.FAIL.value}:
        return value
    return "IDLE"


class RuntimeStateRepository:
    """Write-through persistence for item/task/resource runtime state."""

    def __init__(
        self,
        session_factory,
        *,
        ord_model,
        ord_detail_model,
        ord_stat_model,
        ord_log_model,
        pattern_model,
        item_model,
        equip_task_txn_model,
        insp_task_txn_model=None,
        trans_task_txn_model=None,
        pp_task_txn_model=None,
        equip_stat_model=None,
        trans_stat_model=None,
        chg_location_stat_model=None,
        strg_location_stat_model=None,
        log_event_model=None,
        log_err_equip_model=None,
        log_err_trans_model=None,
        tat_nav_pose_master_model=None,
        trans_task_bat_threshold_model=None,
    ) -> None:
        self._session_factory = session_factory
        self._ord_model = ord_model
        self._ord_detail_model = ord_detail_model
        self._ord_stat_model = ord_stat_model
        self._ord_log_model = ord_log_model
        self._pattern_model = pattern_model
        self._item_model = item_model
        self._equip_task_txn_model = equip_task_txn_model
        self._insp_task_txn_model = insp_task_txn_model
        self._trans_task_txn_model = trans_task_txn_model
        self._pp_task_txn_model = pp_task_txn_model
        self._equip_stat_model = equip_stat_model
        self._trans_stat_model = trans_stat_model
        self._chg_location_stat_model = chg_location_stat_model
        self._strg_location_stat_model = strg_location_stat_model
        self._log_event_model = log_event_model
        self._log_err_equip_model = log_err_equip_model
        self._log_err_trans_model = log_err_trans_model
        self._tat_nav_pose_master_model = tat_nav_pose_master_model
        self._trans_task_bat_threshold_model = trans_task_bat_threshold_model

    @classmethod
    def from_default_db(cls) -> "RuntimeStateRepository":
        from db_session import SessionLocal
        from smart_cast_db.models import (
            ChgLocationStat,
            EquipStat,
            EquipTaskTxn,
            InspTaskTxn,
            ItemStat,
            LogErrEquip,
            LogErrTrans,
            LogEvent,
            Ord,
            OrdDetail,
            OrdLog,
            OrdStat,
            Pattern,
            PpTaskTxn,
            StrgLocationStat,
            TatNavPoseMaster,
            TransStat,
            TransTaskBatThreshold,
            TransTaskTxn,
        )

        return cls(
            SessionLocal,
            ord_model=Ord,
            ord_detail_model=OrdDetail,
            ord_stat_model=OrdStat,
            ord_log_model=OrdLog,
            pattern_model=Pattern,
            item_model=ItemStat,
            equip_task_txn_model=EquipTaskTxn,
            insp_task_txn_model=InspTaskTxn,
            trans_task_txn_model=TransTaskTxn,
            pp_task_txn_model=PpTaskTxn,
            equip_stat_model=EquipStat,
            trans_stat_model=TransStat,
            chg_location_stat_model=ChgLocationStat,
            strg_location_stat_model=StrgLocationStat,
            log_event_model=LogEvent,
            log_err_equip_model=LogErrEquip,
            log_err_trans_model=LogErrTrans,
            tat_nav_pose_master_model=TatNavPoseMaster,
            trans_task_bat_threshold_model=TransTaskBatThreshold,
        )

    def load_tat_nav_poses(self) -> dict[str, tuple[float, float]]:
        """tat_nav_pose_master의 활성 pose (pose_nm → (x, y)) 로드."""
        if self._tat_nav_pose_master_model is None:
            return {}
        with self._session_factory() as db:
            rows = (
                db.query(self._tat_nav_pose_master_model)
                .filter(self._tat_nav_pose_master_model.is_active.is_(True))
                .all()
            )
            return {row.pose_nm: (float(row.pose_x), float(row.pose_y)) for row in rows}

    def load_charger_pose_map(self) -> dict[tuple[int, int], str]:
        """chg_location_stat × tat_nav_pose_master JOIN으로
        (loc_row, loc_col) → pose_nm (ToCHG*) 매핑을 만든다.
        """
        if self._tat_nav_pose_master_model is None or self._chg_location_stat_model is None:
            return {}
        with self._session_factory() as db:
            rows = (
                db.query(
                    self._chg_location_stat_model.loc_row,
                    self._chg_location_stat_model.loc_col,
                    self._tat_nav_pose_master_model.pose_nm,
                )
                .join(
                    self._chg_location_stat_model,
                    self._tat_nav_pose_master_model.loc_id == self._chg_location_stat_model.loc_id,
                )
                .filter(self._tat_nav_pose_master_model.is_active.is_(True))
                .filter(self._tat_nav_pose_master_model.pose_nm.like("ToCHG%"))
                .all()
            )
            return {(int(r.loc_row), int(r.loc_col)): r.pose_nm for r in rows}

    def load_trans_task_bat_thresholds(self) -> dict[TaskType, int]:
        """trans_task_bat_threshold에서 task_type별 최소값을 로드.

        DDL상 (res_id, task_type) 복합키이지만 동일 task_type에 대해 res_id마다 값이
        다를 수 있으므로 가장 보수적인(가장 큰) 임계치를 채택한다.
        """
        if self._trans_task_bat_threshold_model is None:
            return {}
        thresholds: dict[TaskType, int] = {}
        with self._session_factory() as db:
            rows = db.query(self._trans_task_bat_threshold_model).all()
            for row in rows:
                try:
                    task_type = TaskType(row.task_type)
                except ValueError:
                    continue
                value = int(row.bat_low_threshold or 0)
                current = thresholds.get(task_type)
                if current is None or value > current:
                    thresholds[task_type] = value
        return thresholds

    def start_production(self, ord_id: int) -> StartProductionOrderAckModel:
        with self._session_factory() as db:
            ord_obj = db.get(self._ord_model, ord_id)
            if ord_obj is None:
                return StartProductionOrderAckModel(
                    ord_id=ord_id,
                    accepted=False,
                    reason=f"ord_id={ord_id} not found",
                )
            if db.get(self._pattern_model, ord_id) is None:
                return StartProductionOrderAckModel(
                    ord_id=ord_id,
                    accepted=False,
                    reason=f"pattern for ord_id={ord_id} not registered",
                )

            detail = db.query(self._ord_detail_model).filter(self._ord_detail_model.ord_id == ord_id).first()
            if detail is None or int(detail.qty or 0) <= 0:
                return StartProductionOrderAckModel(
                    ord_id=ord_id,
                    accepted=False,
                    reason=f"ord_id={ord_id} has no valid qty in ord_detail",
                )

            existing_item = db.query(self._item_model).filter(self._item_model.ord_id == ord_id).first()
            existing_txn = (
                db.query(self._equip_task_txn_model)
                .join(self._item_model, self._item_model.item_stat_id == self._equip_task_txn_model.item_id)
                .filter(self._item_model.ord_id == ord_id)
                .first()
            )
            if existing_item is not None or existing_txn is not None:
                return StartProductionOrderAckModel(
                    ord_id=ord_id,
                    accepted=False,
                    reason=f"ord_id={ord_id} already started on line",
                    item_id=getattr(existing_item, "item_stat_id", None),
                    equip_task_txn_id=getattr(existing_txn, "txn_id", None),
                )

            stat = db.query(self._ord_stat_model).filter(self._ord_stat_model.ord_id == ord_id).first()
            prev_stat = stat.ord_stat if stat is not None else None
            if stat is None:
                stat = self._ord_stat_model(ord_id=ord_id, ord_stat="MFG")
                db.add(stat)
            else:
                stat.ord_stat = "MFG"
                stat.updated_at = datetime.utcnow()
            if prev_stat != "MFG":
                db.add(
                    self._ord_log_model(
                        ord_id=ord_id,
                        prev_stat=prev_stat,
                        new_stat="MFG",
                        changed_by=None,
                    )
                )

            item_ids: list[int] = []
            created_items: list[Any] = []

            for _ in range(int(detail.qty)):
                new_item = self._item_model(
                    ord_id=ord_id,
                    cur_stat="CREATED",
                    cur_res="PAT",
                    is_defective=None,
                )
                db.add(new_item)
                db.flush()

                new_item_id = getattr(new_item, "item_id", getattr(new_item, "item_stat_id", None))
                created_items.append(new_item)
                item_ids.append(int(new_item_id))

            # 초기 MM equip_task_txn 은 TaskManager.create_next_task(flow_stat=="CREATED") 가
            # StateManager.insert_task_txn 를 통해 한 번만 만든다. 여기서 미리 만들면
            # 같은 item 에 MM txn 두 개가 생성되어 자원 할당 / 상태 추적이 어긋난다.

            db.commit()
            for new_item in created_items:
                db.refresh(new_item)

            return StartProductionOrderAckModel(
                ord_id=ord_id,
                accepted=True,
                reason=f"Production started: {len(item_ids)} items created.",
                item_ids=item_ids,
                equip_task_txn_ids=[],
            )

    def sync_task_created(self, task_meta: dict[str, Any]) -> int | None:
        task_type = _task_db_value(task_meta.get("task_type"))
        if task_type is None:
            return None

        with self._session_factory() as db:
            if task_type in TRANS_TASK_TYPES:
                if self._trans_task_txn_model is None:
                    return None
                txn = self._trans_task_txn_model(
                    trans_id=task_meta.get("res_id"),
                    task_type=task_type,
                    txn_stat=_txn_stat_value(task_meta.get("status")),
                    item_id=task_meta.get("item_id"),
                    ord_id=task_meta.get("ord_id"),
                )
                db.add(txn)
                db.commit()
                db.refresh(txn)
                return int(txn.trans_task_txn_id)

            if task_type == TaskType.INSP.value:
                if self._insp_task_txn_model is None:
                    return None
                txn = self._insp_task_txn_model(
                    txn_stat=_txn_stat_value(task_meta.get("status")),
                    item_id=task_meta.get("item_id"),
                )
                db.add(txn)
                db.commit()
                db.refresh(txn)
                return int(txn.txn_id)

            if task_type == TaskType.PP.value:
                if self._pp_task_txn_model is None:
                    return None
                ord_id = task_meta.get("ord_id")
                if ord_id is None:
                    return None
                txn = self._pp_task_txn_model(
                    ord_id=ord_id,
                    item_id=task_meta.get("item_id"),
                    txn_stat=_txn_stat_value(task_meta.get("status")),
                )
                db.add(txn)
                db.commit()
                db.refresh(txn)
                return int(txn.txn_id)

            txn = self._equip_task_txn_model(
                res_id=task_meta.get("res_id"),
                task_type=task_type,
                txn_stat=_txn_stat_value(task_meta.get("status")),
                item_id=task_meta.get("item_id"),
                strg_loc_id=task_meta.get("strg_loc_id"),
            )
            db.add(txn)
            db.commit()
            db.refresh(txn)
            return int(txn.txn_id)

    def get_order_ptn_loc_id(self, ord_id: int) -> int | None:
        with self._session_factory() as db:
            pattern = db.get(self._pattern_model, ord_id)
            if pattern is None:
                return None
            ptn_loc_id = getattr(pattern, "ptn_loc_id", None)
            return int(ptn_loc_id) if ptn_loc_id is not None else None

    def get_order_target_qty(self, ord_id: int) -> int | None:
        with self._session_factory() as db:
            detail = (
                db.query(self._ord_detail_model)
                .filter(self._ord_detail_model.ord_id == ord_id)
                .first()
            )
            if detail is None:
                return None
            qty = getattr(detail, "qty", None)
            return int(qty) if qty is not None and int(qty) > 0 else None

    def get_storage_slots(self) -> list[dict[str, Any]]:
        if self._strg_location_stat_model is None:
            return []

        with self._session_factory() as db:
            rows = (
                db.query(self._strg_location_stat_model)
                .order_by(
                    self._strg_location_stat_model.loc_row.asc(),
                    self._strg_location_stat_model.loc_col.asc(),
                )
                .all()
            )

            return [
                {
                    "loc_id": int(row.loc_id),
                    "row": int(row.loc_row),
                    "col": int(row.loc_col),
                    "status": str(row.status).capitalize(),
                    "item_id": getattr(row, "item_id", None),
                    "order_id": None,
                }
                for row in rows
            ]

    def reserve_storage_slots(
        self,
        start_row: int,
        start_col: int,
        target_qty: int,
    ) -> int:
        if self._strg_location_stat_model is None or target_qty <= 0:
            return 0

        positions: list[tuple[int, int]] = []
        current_abs_idx = (start_row - 1) * 6 + (start_col - 1)
        for _ in range(target_qty):
            row = (current_abs_idx // 6) + 1
            col = (current_abs_idx % 6) + 1
            positions.append((row, col))
            current_abs_idx += 1

        with self._session_factory() as db:
            slots = (
                db.query(self._strg_location_stat_model)
                .filter(
                    tuple_(
                        self._strg_location_stat_model.loc_row,
                        self._strg_location_stat_model.loc_col,
                    ).in_(positions)
                )
                .all()
            )
            by_pos = {
                (int(slot.loc_row), int(slot.loc_col)): slot
                for slot in slots
            }

            target_slots = [by_pos.get(position) for position in positions]
            if any(slot is None for slot in target_slots):
                return 0
            if any(slot.status != "empty" or slot.item_id is not None for slot in target_slots):
                return 0

            for slot in target_slots:
                slot.status = "reserved"

            db.commit()
            return len(target_slots)

    def update_item_storage_location(self, item_id: int, row: int, col: int) -> None:
        if self._strg_location_stat_model is None:
            return

        with self._session_factory() as db:
            target_slot = (
                db.query(self._strg_location_stat_model)
                .filter(
                    self._strg_location_stat_model.loc_row == row,
                    self._strg_location_stat_model.loc_col == col,
                )
                .first()
            )
            if target_slot is None:
                logger.warning(
                    "[RuntimeStateRepository] storage slot not found: item_id=%s row=%s col=%s",
                    item_id,
                    row,
                    col,
                )
                return

            occupied_slots = (
                db.query(self._strg_location_stat_model)
                .filter(self._strg_location_stat_model.item_id == item_id)
                .all()
            )
            for slot in occupied_slots:
                if slot.loc_id == target_slot.loc_id:
                    continue
                slot.item_id = None
                slot.status = "reserved"

            target_slot.item_id = item_id
            target_slot.status = "occupied"
            db.commit()

    def reserve_charger_slot(self, row: int, col: int) -> bool:
        """Reserve an empty charger slot in chg_location_stat."""
        if self._chg_location_stat_model is None:
            return False

        with self._session_factory() as db:
            slot = (
                db.query(self._chg_location_stat_model)
                .filter(
                    self._chg_location_stat_model.loc_row == row,
                    self._chg_location_stat_model.loc_col == col,
                )
                .first()
            )
            if slot is None:
                logger.warning(
                    "[RuntimeStateRepository] charger slot not found: row=%s col=%s",
                    row,
                    col,
                )
                return False
            if slot.status == "occupied" or slot.res_id is not None:
                return False

            slot.status = "reserved"
            slot.res_id = None
            slot.stored_at = datetime.utcnow()
            db.commit()
            return True

    def occupy_charger_slot(self, res_id: str, row: int, col: int) -> bool:
        """Mark a charger slot occupied by an AMR."""
        if self._chg_location_stat_model is None:
            return False

        with self._session_factory() as db:
            target_slot = (
                db.query(self._chg_location_stat_model)
                .filter(
                    self._chg_location_stat_model.loc_row == row,
                    self._chg_location_stat_model.loc_col == col,
                )
                .first()
            )
            if target_slot is None:
                logger.warning(
                    "[RuntimeStateRepository] charger slot not found: res_id=%s row=%s col=%s",
                    res_id,
                    row,
                    col,
                )
                return False

            occupied_slots = (
                db.query(self._chg_location_stat_model)
                .filter(self._chg_location_stat_model.res_id == res_id)
                .all()
            )
            for slot in occupied_slots:
                if slot.loc_id == target_slot.loc_id:
                    continue
                slot.res_id = None
                slot.status = "empty"
                slot.stored_at = datetime.utcnow()

            target_slot.res_id = res_id
            target_slot.status = "occupied"
            target_slot.stored_at = datetime.utcnow()
            db.commit()
            return True

    def release_charger_slot(
        self,
        row: int | None = None,
        col: int | None = None,
        res_id: str | None = None,
    ) -> bool:
        """Release a reserved/occupied charger slot."""
        if self._chg_location_stat_model is None:
            return False

        with self._session_factory() as db:
            query = db.query(self._chg_location_stat_model)
            if row is not None and col is not None:
                query = query.filter(
                    self._chg_location_stat_model.loc_row == row,
                    self._chg_location_stat_model.loc_col == col,
                )
            elif res_id is not None:
                query = query.filter(self._chg_location_stat_model.res_id == res_id)
            else:
                return False

            slots = query.all()
            if not slots:
                return False

            for slot in slots:
                slot.res_id = None
                slot.status = "empty"
                slot.stored_at = datetime.utcnow()
            db.commit()
            return True

    def sync_task_status(self, task_meta: dict[str, Any]) -> None:
        txn_id = task_meta.get("txn_id") or _task_id_to_int(task_meta.get("task_id"))
        task_type = _task_db_value(task_meta.get("task_type"))
        if txn_id is None or task_type is None:
            return

        status = _txn_stat_value(task_meta.get("status"))
        now = datetime.utcnow()
        with self._session_factory() as db:
            if task_type in TRANS_TASK_TYPES and self._trans_task_txn_model is not None:
                txn = db.get(self._trans_task_txn_model, txn_id)
                if txn is None:
                    return
                txn.txn_stat = status
                txn.trans_id = task_meta.get("res_id")
                if status == TxnStat.PROC.value and txn.start_at is None:
                    txn.start_at = now
                if status in {TxnStat.SUCC.value, TxnStat.FAIL.value}:
                    txn.end_at = now
            elif task_type == TaskType.INSP.value and self._insp_task_txn_model is not None:
                txn = db.get(self._insp_task_txn_model, txn_id)
                if txn is None:
                    return
                txn.txn_stat = status
                if status == TxnStat.PROC.value and txn.start_at is None:
                    txn.start_at = now
                if status in {TxnStat.SUCC.value, TxnStat.FAIL.value}:
                    txn.end_at = now
            elif task_type == TaskType.PP.value and self._pp_task_txn_model is not None:
                txn = db.get(self._pp_task_txn_model, txn_id)
                if txn is None:
                    return
                txn.txn_stat = status
                if status == TxnStat.PROC.value and txn.start_at is None:
                    txn.start_at = now
                if status in {TxnStat.SUCC.value, TxnStat.FAIL.value}:
                    txn.end_at = now
            else:
                txn = db.get(self._equip_task_txn_model, txn_id)
                if txn is None:
                    return
                txn.txn_stat = status
                txn.res_id = task_meta.get("res_id")
                if status == TxnStat.PROC.value and txn.start_at is None:
                    txn.start_at = now
                if status in {TxnStat.SUCC.value, TxnStat.FAIL.value}:
                    txn.end_at = now
            db.commit()

    def record_adapter_result(self, record: dict[str, Any]) -> None:
        """Persist adapter command results into runtime log tables.

        Task txn tables keep coarse lifecycle status. Adapter-specific command
        messages and payloads are stored as event logs; failed physical commands
        also get an error-log row for equipment/transport dashboards.
        """
        txn_id = record.get("txn_id") or _task_id_to_int(record.get("task_id"))
        task_type = _task_db_value(record.get("task_type"))
        detail = json.dumps(record, ensure_ascii=True, default=str)

        with self._session_factory() as db:
            if self._log_event_model is not None:
                db.add(
                    self._log_event_model(
                        component="adapter",
                        event_type="ADAPTER_STEP_RESULT",
                        txn_id=txn_id,
                        detail=detail,
                    )
                )

            if record.get("success") is False and txn_id is not None and task_type is not None:
                message = str(record.get("message") or "adapter_failed")
                failed_stat = str(record.get("action") or task_type)
                if task_type in TRANS_TASK_TYPES and self._log_err_trans_model is not None:
                    db.add(
                        self._log_err_trans_model(
                            res_id=record.get("res_id"),
                            task_txn_id=txn_id,
                            failed_stat=failed_stat,
                            err_msg=message,
                        )
                    )
                elif task_type != TaskType.INSP.value and self._log_err_equip_model is not None:
                    db.add(
                        self._log_err_equip_model(
                            res_id=record.get("res_id"),
                            task_txn_id=txn_id,
                            failed_stat=failed_stat,
                            err_msg=message,
                        )
                    )

            db.commit()

    def sync_item_snapshot(self, item_meta: dict[str, Any]) -> None:
        item_id = item_meta.get("item_id")
        if item_id is None:
            return

        with self._session_factory() as db:
            item = db.get(self._item_model, item_id)
            if item is None:
                return
            if "flow_stat" in item_meta:
                item.cur_stat = item_meta.get("flow_stat")
            if "zone_nm" in item_meta:
                item.cur_res = item_meta.get("zone_nm")
            if item_meta.get("req_res_id") is not None:
                item.cur_res = item_meta.get("req_res_id")
            if item_meta.get("last_task_type") is not None:
                task_type = _task_db_value(item_meta.get("last_task_type"))
                if task_type in TRANS_TASK_TYPES:
                    item.trans_task_type = task_type
                else:
                    item.equip_task_type = task_type
            if "is_defective" in item_meta:
                item.is_defective = item_meta.get("is_defective")
            elif "result" in item_meta:
                result = item_meta.get("result")
                item.is_defective = None if result is None else not bool(result)
            item.updated_at = datetime.utcnow()
            db.commit()

    def sync_resource_snapshot(self, res_meta: dict[str, Any]) -> None:
        res_id = res_meta.get("res_id")
        if res_id is None:
            return

        now = datetime.utcnow()
        item_id = _normalize_item_id(res_meta.get("item_id"))
        with self._session_factory() as db:
            if _res_kind(res_id) == "trans" and self._trans_stat_model is not None:
                stat = db.get(self._trans_stat_model, res_id)
                if stat is None:
                    stat = self._trans_stat_model(res_id=res_id)
                    db.add(stat)
                stat.item_id = item_id
                stat.cur_stat = _trans_stat_value(res_meta.get("status"))
                if res_meta.get("battery_pct") is not None:
                    stat.battery_pct = res_meta.get("battery_pct")
                stat.updated_at = now
            elif self._equip_stat_model is not None:
                stat = db.query(self._equip_stat_model).filter(self._equip_stat_model.res_id == res_id).first()
                if stat is None:
                    stat = self._equip_stat_model(res_id=res_id)
                    db.add(stat)
                stat.item_id = item_id
                stat.txn_type = res_meta.get("task_type")
                stat.cur_stat = _equip_stat_value(res_meta.get("status"))
                stat.updated_at = now
            db.commit()

    def increment_order_gp_qty(self, ord_id: int) -> dict[str, int | bool] | None:
        with self._session_factory() as db:
            detail = db.query(self._ord_detail_model).filter(self._ord_detail_model.ord_id == ord_id).first()
            stat = db.query(self._ord_stat_model).filter(self._ord_stat_model.ord_id == ord_id).first()
            if detail is None or stat is None:
                logger.warning(
                    "[RuntimeStateRepository] PA_GP completion without order detail/stat: ord_id=%s",
                    ord_id,
                )
                return None

            stat.gp_qty = int(stat.gp_qty or 0) + 1
            stat.updated_at = datetime.utcnow()
            db.commit()
            target_qty = int(detail.qty or 0)
            return {
                "complete": stat.gp_qty >= target_qty,
                "gp_qty": int(stat.gp_qty or 0),
                "qty": target_qty,
            }
