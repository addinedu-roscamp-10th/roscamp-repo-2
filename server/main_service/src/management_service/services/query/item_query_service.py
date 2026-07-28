"""Read-side production queries for Management gRPC handlers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import desc, func

from smart_cast_db.database import SessionLocal
from smart_cast_db.models import (
    Equip,
    EquipStat,
    EquipTaskTxn,
    Item,
    Ord,
    OrdPpMap,
    PpOption,
    PpTaskTxn,
    Res,
    Zone,
)

logger = logging.getLogger(__name__)

_LEGACY_STAGE_TO_FLOW_STATS = {
    "QUE": ("CREATED", "WAIT_INSP", "WAIT_PA"),
    "MM": ("CAST",),
    "DM": tuple(),
    "TR_PP": ("WAIT_PP",),
    "PP": ("PP", "PA"),
    "IP": ("INSP",),
    "TR_LD": ("STORED", "PICK"),
    "SH": ("READY_TO_SHIP", "DISCARDED"),
}

_FLOW_TO_LEGACY_STAGE = {
    "CREATED": "QUE",
    "CAST": "MM",
    "WAIT_PP": "TR_PP",
    "PP": "PP",
    "WAIT_INSP": "QUE",
    "INSP": "IP",
    "WAIT_PA": "QUE",
    "PA": "PP",
    "STORED": "TR_LD",
    "PICK": "TR_LD",
    "READY_TO_SHIP": "SH",
    "DISCARDED": "SH",
}


def _legacy_stage_from_flow(flow_stat: str | None) -> str:
    return _FLOW_TO_LEGACY_STAGE.get((flow_stat or "").upper(), "QUE")


@dataclass(frozen=True)
class ItemQueryRow:
    """Projection used by ListItems RPC and legacy-compatible read paths."""

    item_id: int
    ord_id: int
    flow_stat: str
    zone_nm: str
    result: bool | None
    cur_stat: str
    cur_res: str
    is_defective: bool | None
    updated_at: datetime | None


@dataclass(frozen=True)
class EquipTaskQueryRow:
    txn_id: int
    res_id: str | None
    task_type: str | None
    txn_stat: str | None
    item_id: int | None
    strg_loc_id: int | None
    ship_loc_id: int | None
    req_at: datetime | None
    start_at: datetime | None
    end_at: datetime | None


@dataclass(frozen=True)
class PpOptionQueryRow:
    pp_id: int
    pp_nm: str | None
    extra_cost: float | None


@dataclass(frozen=True)
class PpTaskStatusQueryRow:
    txn_id: int
    ord_id: int
    map_id: int | None
    pp_nm: str | None
    item_id: int | None
    operator_id: int | None
    txn_stat: str | None
    req_at: datetime | None
    start_at: datetime | None
    end_at: datetime | None


@dataclass(frozen=True)
class ItemPpRequirementsQueryRow:
    item_id: int
    ord_id: int
    pp_options: list[PpOptionQueryRow]
    pp_task_status: list[PpTaskStatusQueryRow]


@dataclass(frozen=True)
class EquipmentQueryRow:
    res_id: str
    res_type: str | None
    model_nm: str | None
    zone_id: int | None
    cur_stat: str | None
    err_msg: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class StageQueryRow:
    zone_id: int
    zone_nm: str
    in_progress_count: int


@dataclass(frozen=True)
class OrderItemProgressQueryRow:
    ord_id: int
    total_items: int
    by_stat: dict[str, int]


class ItemQueryService:
    """Read-only item queries backed by the shared smartcast database."""

    def list_items(
        self,
        order_id: str | None,
        stage: str | None,
        limit: int,
    ) -> list[ItemQueryRow]:
        with SessionLocal() as db:
            q = db.query(Item)
            if order_id:
                try:
                    q = q.filter(Item.ord_id == int(order_id))
                except (TypeError, ValueError):
                    logger.warning("list_items: invalid order_id=%r - filter ignored", order_id)
            if stage:
                flow_stats = _LEGACY_STAGE_TO_FLOW_STATS.get(stage, ())
                if not flow_stats:
                    return []
                q = q.filter(Item.flow_stat.in_(flow_stats))
            rows = q.order_by(Item.updated_at.desc(), Item.item_id.asc()).limit(limit or 100).all()
            return [
                ItemQueryRow(
                    item_id=row.item_id,
                    ord_id=row.ord_id,
                    flow_stat=row.flow_stat or "",
                    zone_nm=row.zone_nm or "",
                    result=row.result,
                    cur_stat=_legacy_stage_from_flow(row.flow_stat),
                    cur_res=row.cur_res or "",
                    is_defective=None if row.result is None else (not row.result),
                    updated_at=row.updated_at,
                )
                for row in rows
            ]

    def list_equip_tasks(
        self,
        *,
        res_id: str | None = None,
        item_id: int | None = None,
        limit: int = 100,
    ) -> list[EquipTaskQueryRow]:
        with SessionLocal() as db:
            q = db.query(EquipTaskTxn)
            if res_id:
                q = q.filter(EquipTaskTxn.res_id == res_id)
            if item_id and item_id > 0:
                q = q.filter(EquipTaskTxn.item_id == item_id)
            rows = q.order_by(desc(EquipTaskTxn.req_at), desc(EquipTaskTxn.txn_id)).limit(limit).all()
            return [
                EquipTaskQueryRow(
                    txn_id=row.txn_id,
                    res_id=row.res_id,
                    task_type=row.task_type,
                    txn_stat=row.txn_stat,
                    item_id=row.item_id,
                    strg_loc_id=row.strg_loc_id,
                    ship_loc_id=row.ship_loc_id,
                    req_at=row.req_at,
                    start_at=row.start_at,
                    end_at=row.end_at,
                )
                for row in rows
            ]

    def get_item_pp_requirements(self, item_id: int) -> ItemPpRequirementsQueryRow | None:
        with SessionLocal() as db:
            item = db.get(Item, item_id)
            if item is None:
                return None
            pp_opts = (
                db.query(PpOption)
                .join(OrdPpMap, OrdPpMap.pp_id == PpOption.pp_id)
                .filter(OrdPpMap.ord_id == item.ord_id)
                .order_by(PpOption.pp_id.asc())
                .all()
            )
            txns = (
                db.query(PpTaskTxn)
                .filter(PpTaskTxn.item_id == item_id)
                .order_by(desc(PpTaskTxn.req_at), desc(PpTaskTxn.txn_id))
                .all()
            )
            return ItemPpRequirementsQueryRow(
                item_id=item_id,
                ord_id=item.ord_id,
                pp_options=[
                    PpOptionQueryRow(
                        pp_id=opt.pp_id,
                        pp_nm=opt.pp_nm,
                        extra_cost=float(opt.extra_cost) if opt.extra_cost is not None else None,
                    )
                    for opt in pp_opts
                ],
                pp_task_status=[
                    PpTaskStatusQueryRow(
                        txn_id=txn.txn_id,
                        ord_id=txn.ord_id,
                        map_id=txn.map_id,
                        pp_nm=txn.pp_nm,
                        item_id=txn.item_id,
                        operator_id=txn.operator_id,
                        txn_stat=txn.txn_stat,
                        req_at=txn.req_at,
                        start_at=txn.start_at,
                        end_at=txn.end_at,
                    )
                    for txn in txns
                ],
            )

    def list_equipment(self) -> list[EquipmentQueryRow]:
        with SessionLocal() as db:
            rows: list[EquipmentQueryRow] = []
            for res in db.query(Res).order_by(Res.res_id.asc()).all():
                latest = (
                    db.query(EquipStat)
                    .filter(EquipStat.res_id == res.res_id)
                    .order_by(desc(EquipStat.updated_at), desc(EquipStat.stat_id))
                    .first()
                )
                equip = db.get(Equip, res.res_id)
                rows.append(
                    EquipmentQueryRow(
                        res_id=res.res_id,
                        res_type=res.res_type,
                        model_nm=res.model_nm,
                        zone_id=equip.zone_id if equip is not None else None,
                        cur_stat=latest.cur_stat if latest is not None else None,
                        err_msg=latest.err_msg if latest is not None else None,
                        updated_at=latest.updated_at if latest is not None else None,
                    )
                )
            return rows

    def list_stages(self) -> list[StageQueryRow]:
        with SessionLocal() as db:
            rows: list[StageQueryRow] = []
            for zone in db.query(Zone).order_by(Zone.zone_id.asc()).all():
                in_progress = (
                    db.query(func.count(Item.item_id))
                    .filter(Item.zone_nm == zone.zone_nm)
                    .filter(Item.flow_stat.notin_(["READY_TO_SHIP", "DISCARDED"]))
                    .scalar()
                    or 0
                )
                rows.append(
                    StageQueryRow(
                        zone_id=zone.zone_id,
                        zone_nm=zone.zone_nm,
                        in_progress_count=int(in_progress),
                    )
                )
            return rows

    def list_order_item_progress(self) -> list[OrderItemProgressQueryRow]:
        with SessionLocal() as db:
            rows: list[OrderItemProgressQueryRow] = []
            for ord_row in db.query(Ord).order_by(Ord.ord_id.asc()).all():
                items = db.query(Item).filter(Item.ord_id == ord_row.ord_id).all()
                stat_counts: dict[str, int] = {}
                for item in items:
                    key = _legacy_stage_from_flow(item.flow_stat)
                    stat_counts[key] = stat_counts.get(key, 0) + 1
                rows.append(
                    OrderItemProgressQueryRow(
                        ord_id=ord_row.ord_id,
                        total_items=len(items),
                        by_stat=stat_counts,
                    )
                )
            return rows
