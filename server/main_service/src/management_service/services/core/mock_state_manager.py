"""Minimal in-memory StateManager stub for the refactor phase.

This module intentionally avoids any DB or ORM dependency. During the current
core-structure refactor, Management only needs to acknowledge StartProduction
requests and keep the rest of the pipeline dormant.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from ..contracts.enums import EventType
from ..contracts.enums import TaskType, TxnStat
from ..contracts.models import (
    AmrLocationResult,
    AllocateTaskResInput,
    CreateTaskInput,
    Event,
    ItemStatusRecord,
    StartProductionOrderAckModel,
    UpdateTaskStatusInput,
)

logger = logging.getLogger(__name__)
ITEM_AFFINITY_PREDECESSORS = {"MM", "POUR", "PP", "ToINSP", "INSP"}


def _normalize_task_type(task_type: Any) -> TaskType | None:
    if isinstance(task_type, TaskType):
        return task_type
    if isinstance(task_type, str):
        try:
            return TaskType(task_type)
        except ValueError:
            return None
    return None


class MockStateManager:
    """StateManager stub that only acknowledges production-start requests."""

    def __init__(self, event_bridge=None) -> None:
        self._items: dict[int, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._res_list: dict[str, dict[str, Any]] = {}
        self.items = self._items
        self.orders: dict[int, dict[str, Any]] = {}
        self.slot_table: dict[tuple, dict[str, Any]] = {}
        self._event_bridge = event_bridge
        self._next_item_id = 1000
        self._next_equip_task_txn_id = 2000
        self._db_ready = False
        self._session_factory = None
        self._ord_model = None
        self._ord_detail_model = None
        self._ord_stat_model = None
        self._ord_log_model = None
        self._pattern_model = None
        self._item_model = None
        self._equip_task_txn_model = None
        self._seed_res_pool()
        try:
            from db_session import SessionLocal
            from smart_cast_db.models import EquipTaskTxn, ItemStat, Ord, OrdDetail, OrdLog, OrdStat, Pattern

            self._session_factory = SessionLocal
            self._ord_model = Ord
            self._ord_detail_model = OrdDetail
            self._ord_stat_model = OrdStat
            self._ord_log_model = OrdLog
            self._pattern_model = Pattern
            self._item_model = ItemStat
            self._equip_task_txn_model = EquipTaskTxn
            self._db_ready = True
            logger.info("[MockStateManager] DB-backed start_production enabled")
        except Exception as exc:
            logger.warning("[MockStateManager] DB-backed start_production unavailable: %s", exc)
        logger.info("[MockStateManager] stub mode enabled")

    def _seed_res_pool(self) -> None:
        for res_type in ("TAT", "CONV", "RA_STRG", "RA_CAST"):
            for idx in range(1, 5):
                res_id = f"{res_type}_{idx}"
                self._res_list[res_id] = {
                    "res_id": res_id,
                    "res_type": res_type,
                    "task_id": None,
                    "item_id": None,
                    "status": "idle",
                    "x": float(idx - 1) if res_type == "TAT" else 0.0,
                    "y": 0.0,
                }

    async def start_production(self, ord_id: int) -> StartProductionOrderAckModel:
        """Accept a positive order id and return a deterministic mock ack."""
        logger.info("[MockStateManager] start_production called for ord_id=%s", ord_id)
        if ord_id <= 0:
            return StartProductionOrderAckModel(
                ord_id=ord_id,
                accepted=False,
                reason="Invalid Order ID (Mock)",
            )

        if self._db_ready:
            return self._start_production_db(ord_id)

        target_qty = max(int(self.orders.get(ord_id, {}).get("target", 1) or 1), 1)
        item_ids: list[int] = []
        equip_task_txn_ids: list[int] = []

        for _ in range(target_qty):
            item_id = self._next_item_id
            equip_task_txn_id = self._next_equip_task_txn_id
            self._next_item_id += 1
            self._next_equip_task_txn_id += 1

            self._items[item_id] = {
                "item_id": item_id,
                "ord_id": ord_id,
                "order_id": ord_id,
                "flow_stat": "CREATED",
                "zone_nm": None,
                "result": None,
            }
            # self._tasks[f"task_{equip_task_txn_id}"] = {
            #     "ord_id": ord_id,
            #     "item_id": item_id,
                # "txn_id": equip_task_txn_id,
                # "status": "QUE",
                # "task_type": "MM",
                # "res_id": "PAT",
            # }
            item_ids.append(item_id)
            # equip_task_txn_ids.append(equip_task_txn_id)

        return StartProductionOrderAckModel(
            ord_id=ord_id,
            accepted=True,
            reason=f"Accepted by MockStateManager. created_items={len(item_ids)}",
            item_ids=item_ids,
            equip_task_txn_ids=[],
        )

    def _start_production_db(self, ord_id: int) -> StartProductionOrderAckModel:
        if (
            self._session_factory is None
            or self._ord_model is None
            or self._ord_detail_model is None
            or self._ord_stat_model is None
            or self._ord_log_model is None
            or self._pattern_model is None
            or self._item_model is None
            or self._equip_task_txn_model is None
        ):
            return StartProductionOrderAckModel(
                ord_id=ord_id,
                accepted=False,
                reason="DB-backed start_production is not fully initialized.",
            )

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
            equip_task_txn_ids: list[int] = []
            created_items: list[Any] = []
            created_txns: list[Any] = []

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
                txn = self._equip_task_txn_model(
                    res_id="PAT",
                    task_type="MM",
                    txn_stat="QUE",
                    item_id=new_item_id,
                )
                db.add(txn)
                db.flush()

                created_items.append(new_item)
                created_txns.append(txn)
                item_ids.append(int(new_item_id))
                equip_task_txn_ids.append(int(txn.txn_id))

            db.commit()
            for new_item, txn in zip(created_items, created_txns):
                db.refresh(new_item)
                db.refresh(txn)
                new_item_id = getattr(new_item, "item_id", getattr(new_item, "item_stat_id", None))
                self._items[new_item_id] = {
                    "item_id": new_item_id,
                    "ord_id": ord_id,
                    "order_id": ord_id,
                    "flow_stat": getattr(new_item, "cur_stat", "CREATED"),
                    "zone_nm": getattr(new_item, "cur_res", None),
                    "result": getattr(new_item, "result", None),
                }
                self._tasks[f"task_{txn.txn_id}"] = {
                    "ord_id": ord_id,
                    "item_id": new_item_id,
                    "txn_id": txn.txn_id,
                    "status": txn.txn_stat,
                    "res_id": txn.res_id,
                    "task_type": txn.task_type,
                }

            return StartProductionOrderAckModel(
                ord_id=ord_id,
                accepted=True,
                reason=f"Production started: {len(item_ids)} items and MM tasks created.",
                item_ids=item_ids,
                equip_task_txn_ids=equip_task_txn_ids,
            )

    def create_order_with_items(self, ord_id: int, qty: int) -> list[int]:
        item_ids: list[int] = []
        for _ in range(max(qty, 0)):
            item_id = self._next_item_id
            self._next_item_id += 1
            self._items[item_id] = {
                "item_id": item_id,
                "ord_id": ord_id,
                "order_id": ord_id,
                "flow_stat": "CREATED",
                "zone_nm": None,
                "result": None,
            }
            item_ids.append(item_id)
        return item_ids

    def find_ship_ready_item_ids(
        self,
        ord_id: int | None = None,
        item_ids: list[int] | None = None,
    ) -> list[int]:
        if item_ids is not None:
            return item_ids
        if ord_id is None:
            return []
        return [
            item_id
            for item_id, item in self._items.items()
            if item.get("ord_id") == ord_id
        ]

    async def get_item(self, item_id: int) -> ItemStatusRecord:
        item = dict(self._items.get(item_id, {"item_id": item_id, "flow_stat": "HOLD"}))
        last_task_type = item.get("last_task_type") or item.get("task_type")
        if isinstance(last_task_type, str):
            try:
                last_task_type = TaskType(last_task_type)
            except ValueError:
                last_task_type = None
        else:
            last_task_type = None

        return ItemStatusRecord(
            item_id=int(item["item_id"]),
            order_id=int(item.get("order_id") or item.get("ord_id") or 0),
            last_task_type=last_task_type,
            req_res_id=item.get("req_res_id"),
            flow_stat=item.get("flow_stat"),
            is_defective=bool(item.get("is_defective", False)),
            ptn_id=item.get("ptn_id"),
        )

    async def insert_task_txn(self, task_input: CreateTaskInput) -> int:
        txn_id = self._next_equip_task_txn_id
        self._next_equip_task_txn_id += 1
        task_id = f"task_{txn_id}"
        item_meta = self._items.setdefault(task_input.item_id, {"item_id": task_input.item_id})
        self._tasks[task_id] = {
            "txn_id": txn_id,
            "item_id": task_input.item_id,
            "ord_id": item_meta.get("ord_id") or item_meta.get("order_id"),
            "task_type": task_input.task_type.value,
            "status": str(task_input.txn_stat),
            "res_id": task_input.res_id,
            "strg_loc": task_input.strg_loc,
        }
        return txn_id

    async def create_empty_item(self, order_id: int) -> int:
        item_id = self._next_item_id
        self._next_item_id += 1
        self._items[item_id] = {
            "item_id": item_id,
            "ord_id": order_id,
            "order_id": order_id,
            "flow_stat": "CREATED",
            "zone_nm": None,
            "result": None,
        }
        return item_id

    def add_task(self, task: dict[str, Any]) -> str:
        task_id = f"task_{len(self._tasks) + 1}"
        self._tasks[task_id] = dict(task)
        return task_id

    def find_available_res(self, res_type: str, task_type: str | None = None) -> str | None:
        for res_id, res_meta in self._res_list.items():
            if (
                res_meta.get("res_type") == res_type
                and res_meta.get("status") == "idle"
                and res_meta.get("item_id") is None
            ):
                return res_id
        return None

    async def get_available_resources(self, req_res_type: str) -> list[str]:
        available = [
            res_id
            for res_id, res_meta in sorted(self._res_list.items())
            if res_meta.get("res_type") == req_res_type
            and res_meta.get("status") == "idle"
            and res_meta.get("item_id") is None
        ]
        return available

    async def get_amr_locations(self) -> list[AmrLocationResult]:
        return [
            AmrLocationResult(
                res_id=res_id,
                x=float(res_meta.get("x", 0.0)),
                y=float(res_meta.get("y", 0.0)),
            )
            for res_id, res_meta in sorted(self._res_list.items())
            if res_meta.get("res_type") == "TAT"
        ]

    def get_res_available_for_item(self, res_id: str, item_id: int | None = None) -> bool:
        res_meta = self._res_list.get(res_id)
        if res_meta is None:
            return False
        if res_meta.get("status") != "idle":
            return False
        if item_id is None:
            return res_meta.get("item_id") is None
        return res_meta.get("item_id") == item_id

    async def update_task_allocation(self, assign_input: AllocateTaskResInput) -> None:
        task_key = (
            assign_input.task_id
            if assign_input.task_id.startswith("task_")
            else f"task_{assign_input.task_id}"
        )
        task_meta = self._tasks.get(task_key)
        if task_meta is not None:
            task_meta["item_id"] = assign_input.item_id
            task_meta["res_id"] = assign_input.res_id
            task_meta["assignment_status"] = "allocated"
            task_meta["status"] = "allocated"

        self._res_list.setdefault(assign_input.res_id, {})
        self._res_list[assign_input.res_id].update(
            {
                "res_id": assign_input.res_id,
                "res_type": self._res_list.get(assign_input.res_id, {}).get("res_type"),
                "task_id": assign_input.task_id,
                "item_id": assign_input.item_id,
                "status": "allocated",
            }
        )
        logger.info(
            "[MockStateManager] update_task_allocation: task=%s item=%s res=%s",
            assign_input.task_id,
            assign_input.item_id,
            assign_input.res_id,
        )

    async def update_task_status(self, req: UpdateTaskStatusInput) -> bool:
        task_key = req.task_id if req.task_id.startswith("task_") else f"task_{req.task_id}"
        task_meta = self._tasks.get(task_key)
        previous_status = task_meta.get("status") if task_meta is not None else None
        suppress_task_completed = False
        suppress_resource_available = False
        assigned_res_id = task_meta.get("res_id") if task_meta is not None else None
        if task_meta is not None:
            task_meta["status"] = req.new_stat.value
            if req.error_code is not None:
                task_meta["error_code"] = req.error_code
            if req.new_stat == TxnStat.PROC and assigned_res_id is not None:
                item_id = task_meta.get("item_id")
                if item_id is not None and task_meta.get("task_type") == "MM":
                    item = self._items.setdefault(item_id, {"item_id": item_id})
                    if item.get("flow_stat") == "CREATED":
                        item["flow_stat"] = "CAST"
                res_meta = self._res_list.setdefault(assigned_res_id, {"res_id": assigned_res_id})
                res_meta.update(
                    {
                        "task_id": req.task_id,
                        "item_id": task_meta.get("item_id"),
                        "status": TxnStat.PROC.value,
                    }
                )
            if req.new_stat == TxnStat.SUCC:
                item_id = task_meta.get("item_id")
                if item_id is not None:
                    item = self._items.setdefault(item_id, {"item_id": item_id})
                    item["last_task_type"] = task_meta.get("task_type")
                    item["req_res_id"] = (
                        task_meta.get("res_id")
                        if task_meta.get("task_type") in ITEM_AFFINITY_PREDECESSORS
                        else None
                    )
                if task_meta.get("task_type") == "ToSTRG":
                    suppress_task_completed = True
                if previous_status != TxnStat.SUCC.value and task_meta.get("task_type") == "PA_GP":
                    suppress_task_completed = self._handle_pa_gp_completion(task_meta)
            if req.new_stat in {TxnStat.SUCC, TxnStat.FAIL} and assigned_res_id is not None:
                res_meta = self._res_list.setdefault(assigned_res_id, {"res_id": assigned_res_id})
                keep_item_affinity = (
                    req.new_stat == TxnStat.SUCC
                    and task_meta.get("task_type") in ITEM_AFFINITY_PREDECESSORS
                )
                res_meta.update(
                    {
                        "task_id": None,
                        "status": "idle",
                    }
                )
                if keep_item_affinity:
                    res_meta["item_id"] = task_meta.get("item_id")
                    suppress_resource_available = True
                else:
                    res_meta["item_id"] = None

                req_res_type = res_meta.get("res_type")
                if (
                    self._event_bridge is not None
                    and isinstance(req_res_type, str)
                    and req_res_type
                    and not suppress_resource_available
                ):
                    self._event_bridge.publish(
                        Event(
                            event_type=EventType.RESOURCE_AVAILABLE,
                            item_id=task_meta.get("item_id"),
                            res_id=assigned_res_id,
                            payload={
                                "req_res_type": req_res_type,
                                "task_id": req.task_id,
                                "status": req.new_stat.value,
                            },
                        )
                    )
        logger.info(
            "[MockStateManager] update_task_status: task=%s status=%s",
            req.task_id,
            req.new_stat.value,
        )
        if (
            req.new_stat in {TxnStat.SUCC, TxnStat.FAIL}
            and self._event_bridge is not None
            and task_meta is not None
            and not suppress_task_completed
        ):
            task_type = _normalize_task_type(task_meta.get("task_type"))
            self._event_bridge.publish(
                Event(
                    event_type=EventType.TASK_COMPLETED,
                    txn_id=task_meta.get("txn_id"),
                    ord_id=task_meta.get("ord_id"),
                    item_id=task_meta.get("item_id"),
                    res_id=task_meta.get("res_id"),
                    payload={
                        "task_id": req.task_id,
                        "status": req.new_stat.value,
                        "task_type": task_type,
                    },
                )
            )
        return True

    def _handle_pa_gp_completion(self, task_meta: dict[str, Any]) -> bool:
        ord_id = task_meta.get("ord_id")
        if ord_id is None:
            return False

        if self._db_ready and self._session_factory and self._ord_detail_model and self._ord_stat_model:
            with self._session_factory() as db:
                detail = db.query(self._ord_detail_model).filter(self._ord_detail_model.ord_id == ord_id).first()
                stat = db.query(self._ord_stat_model).filter(self._ord_stat_model.ord_id == ord_id).first()
                if detail is None or stat is None:
                    logger.warning(
                        "[MockStateManager] PA_GP completion without order detail/stat: ord_id=%s",
                        ord_id,
                    )
                    return False

                stat.gp_qty = int(stat.gp_qty or 0) + 1
                stat.updated_at = datetime.utcnow()
                db.commit()

                is_complete = stat.gp_qty >= int(detail.qty or 0)
                logger.info(
                    "[MockStateManager] PA_GP completion: ord_id=%s gp_qty=%s qty=%s complete=%s",
                    ord_id,
                    stat.gp_qty,
                    detail.qty,
                    is_complete,
                )
                if is_complete:
                    logger.info(
                        "[MockStateManager] ord_id=%s qty=%s 생산완료",
                        ord_id,
                        detail.qty,
                    )
                return is_complete

        order_state = self.orders.setdefault(ord_id, {})
        gp_qty = int(order_state.get("gp_qty", 0)) + 1
        order_state["gp_qty"] = gp_qty
        target_qty = order_state.get("target")
        is_complete = target_qty is not None and gp_qty >= int(target_qty)
        logger.info(
            "[MockStateManager] PA_GP completion(memory): ord_id=%s gp_qty=%s qty=%s complete=%s",
            ord_id,
            gp_qty,
            target_qty,
            is_complete,
        )
        if is_complete:
            logger.info(
                "[MockStateManager] ord_id=%s qty=%s 생산완료",
                ord_id,
                target_qty,
            )
        return is_complete

    async def publish_subtask_completed(
        self,
        *,
        task_id: str,
        item_id: int | None,
        subtask_type: str,
        task_type: TaskType | None = None,
    ) -> bool:
        if self._event_bridge is None:
            return False

        task_key = task_id if task_id.startswith("task_") else f"task_{task_id}"
        task_meta = self._tasks.get(task_key, {})
        payload_task_type = task_type or _normalize_task_type(task_meta.get("task_type"))
        self._event_bridge.publish(
            Event(
                event_type=EventType.SUBTASK_COMPLETED,
                txn_id=task_meta.get("txn_id"),
                ord_id=task_meta.get("ord_id"),
                item_id=item_id if item_id is not None else task_meta.get("item_id"),
                res_id=task_meta.get("res_id"),
                payload={
                    "task_id": task_id,
                    "subtask_type": subtask_type,
                    "task_type": payload_task_type,
                },
            )
        )
        logger.info(
            "[MockStateManager] publish_subtask_completed: task=%s subtask_type=%s item_id=%s",
            task_id,
            subtask_type,
            item_id if item_id is not None else task_meta.get("item_id"),
        )
        return True

    async def publish_amr_charged(
        self,
        *,
        res_id: str,
        task_id: str | None = None,
        item_id: int | None = None,
        source: str | None = None,
    ) -> bool:
        if self._event_bridge is None:
            return False

        res_meta = self._res_list.setdefault(res_id, {})
        res_meta["res_id"] = res_id
        res_meta["status"] = "idle"
        if task_id is not None:
            res_meta["task_id"] = task_id
        if item_id is not None:
            res_meta["item_id"] = item_id

        self._event_bridge.publish(
            Event(
                event_type=EventType.AMR_CHARGED,
                item_id=item_id,
                res_id=res_id,
                payload={
                    "task_id": task_id,
                    "source": source,
                },
            )
        )
        logger.info(
            "[MockStateManager] publish_amr_charged: resource=%s task=%s source=%s",
            res_id,
            task_id,
            source,
        )
        return True

    def mark_task_started(self, task_id: str, res_id: str, is_trans: bool) -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["status"] = "PROC"
        res_meta = self._res_list.setdefault(res_id, {"res_id": res_id})
        res_meta.update({"task_id": task_id, "status": "PROC"})
        logger.info("[MockStateManager] mark_task_started: task=%s res=%s", task_id, res_id)

    def update_item_status(
        self,
        item_id: int,
        flow_stat: str | None = None,
        zone_nm: str | None = None,
        result: bool | None = None,
    ) -> None:
        item = self._items.setdefault(item_id, {"item_id": item_id})
        if flow_stat is not None:
            item["flow_stat"] = flow_stat
        if zone_nm is not None:
            item["zone_nm"] = zone_nm
        if result is not None:
            item["result"] = result
        logger.info(
            "[MockStateManager] update_item_status: item=%s flow_stat=%s zone_nm=%s",
            item_id,
            flow_stat,
            zone_nm,
        )

    def update_res_status_memory(self, res_id: str, x: float, y: float, battery_pct: int) -> None:
        logger.debug(
            "[MockStateManager] update_res_status_memory: res=%s x=%s y=%s battery=%s",
            res_id,
            x,
            y,
            battery_pct,
        )

    def update_amr_runtime_memory(
        self,
        res_id: str,
        *,
        x: float | None = None,
        y: float | None = None,
        battery_pct: int | None = None,
    ) -> None:
        logger.debug(
            "[MockStateManager] update_amr_runtime_memory: res=%s x=%s y=%s battery=%s",
            res_id,
            x,
            y,
            battery_pct,
        )

    def update_res_task_state(self, task_id: str, res_id: str, cur_stat: str) -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["status"] = cur_stat
        res_meta = self._res_list.setdefault(res_id, {"res_id": res_id})
        res_meta.update(
            {
                "task_id": None if cur_stat in {TxnStat.SUCC.value, TxnStat.FAIL.value} else task_id,
                "status": "idle" if cur_stat in {TxnStat.SUCC.value, TxnStat.FAIL.value} else cur_stat,
            }
        )
        logger.info(
            "[MockStateManager] update_res_task_state: task=%s res=%s cur_stat=%s",
            task_id,
            res_id,
            cur_stat,
        )
