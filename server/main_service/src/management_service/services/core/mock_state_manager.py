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
from ..contracts.enums import TxnStat
from ..contracts.models import (
    AmrLocationResult,
    AssignTaskRobotInput,
    CreateTaskInput,
    Event,
    StartProductionOrderAckModel,
    UpdateTaskStatusInput,
)

logger = logging.getLogger(__name__)


class MockStateManager:
    """StateManager stub that only acknowledges production-start requests."""

    def __init__(self, event_bridge=None) -> None:
        self._items: dict[int, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._robots: dict[str, dict[str, Any]] = {}
        self.items = self._items
        self.orders: dict[int, dict[str, Any]] = {}
        self.slot_table: dict[tuple, dict[str, Any]] = {}
        self._event_bridge = event_bridge
        self._next_item_id = 1000
        self._next_equip_task_txn_id = 2000
        self._db_ready = False
        self._session_factory = None
        self._ord_model = None
        self._ord_stat_model = None
        self._ord_log_model = None
        self._pattern_model = None
        self._item_model = None
        self._equip_task_txn_model = None
        try:
            from db_session import SessionLocal
            from smart_cast_db.models import EquipTaskTxn, ItemStat, Ord, OrdLog, OrdStat, Pattern

            self._session_factory = SessionLocal
            self._ord_model = Ord
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
        self._tasks[f"task_{equip_task_txn_id}"] = {
            "ord_id": ord_id,
            "item_id": item_id,
            "txn_id": equip_task_txn_id,
            "status": "QUE",
        }

        return StartProductionOrderAckModel(
            ord_id=ord_id,
            accepted=True,
            reason="Accepted by MockStateManager.",
            item_id=item_id,
            equip_task_txn_id=equip_task_txn_id,
        )

    def _start_production_db(self, ord_id: int) -> StartProductionOrderAckModel:
        if (
            self._session_factory is None
            or self._ord_model is None
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

            new_item = self._item_model(
                ord_id=ord_id,
                cur_stat="CREATED",
                cur_res="PAT",
                is_defective=None,
            )
            db.add(new_item)
            db.flush()

            txn = self._equip_task_txn_model(
                res_id="PAT",
                task_type="MM",
                txn_stat="QUE",
                item_id=new_item.item_id,
            )
            db.add(txn)
            db.commit()
            db.refresh(new_item)
            db.refresh(txn)

            self._items[new_item.item_id] = {
                "item_id": new_item.item_id,
                "ord_id": ord_id,
                "order_id": ord_id,
                "flow_stat": getattr(new_item, "cur_stat", "CREATED"),
                "zone_nm": getattr(new_item, "cur_res", None),
                "result": getattr(new_item, "result", None),
            }
            self._tasks[f"task_{txn.txn_id}"] = {
                "ord_id": ord_id,
                "item_id": new_item.item_id,
                "txn_id": txn.txn_id,
                "status": txn.txn_stat,
                "res_id": txn.res_id,
            }

            return StartProductionOrderAckModel(
                ord_id=ord_id,
                accepted=True,
                reason="Production started: item and equip_task_txn created.",
                item_id=new_item.item_id,
                equip_task_txn_id=txn.txn_id,
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

    def get_item(self, item_id: int) -> dict[str, Any]:
        item = dict(self._items.get(item_id, {"item_id": item_id, "flow_stat": "HOLD"}))
        flow_stat = item.get("flow_stat")
        zone_nm = item.get("zone_nm")
        result = item.get("result")
        item.setdefault("order_id", item.get("ord_id"))
        item.setdefault("cur_stat", flow_stat)
        item.setdefault("cur_res", zone_nm)
        item.setdefault("is_defective", None if result is None else (not result))
        return item

    def insert_task_txn(self, task_input: CreateTaskInput) -> int:
        txn_id = self._next_equip_task_txn_id
        self._next_equip_task_txn_id += 1
        task_id = f"task_{txn_id}"
        self._tasks[task_id] = {
            "txn_id": txn_id,
            "item_id": task_input.item_id,
            "task_type": task_input.task_type.value,
            "status": str(task_input.txn_stat),
            "res_id": task_input.res_id,
            "strg_loc": task_input.strg_loc,
        }
        item = self._items.setdefault(task_input.item_id, {"item_id": task_input.item_id})
        item["last_task_type"] = task_input.task_type.value
        return txn_id

    def create_empty_item(self, order_id: int) -> int:
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

    def find_available_robot(self, robot_type: str, task_type: str | None = None) -> str | None:
        return "robot_1"

    async def get_available_resources(self, req_res_type: str) -> list[str]:
        defaults = {
            "TAT": ["TAT_1"],
            "CONV": ["CONV_1"],
            "RA_STRG": ["RA_STRG_1"],
            "RA_CAST": ["RA_CAST_1"],
        }
        return defaults.get(req_res_type, ["robot_1"])

    async def get_amr_locations(self) -> list[AmrLocationResult]:
        return [AmrLocationResult(res_id="TAT_1", x=0.0, y=0.0)]

    def get_robot_available_for_item(self, robot_id: str, item_id: int | None = None) -> bool:
        return True

    def update_task_allocation(self, assign_input: AssignTaskRobotInput) -> None:
        task_key = (
            assign_input.task_id
            if assign_input.task_id.startswith("task_")
            else f"task_{assign_input.task_id}"
        )
        task_meta = self._tasks.get(task_key)
        if task_meta is not None:
            task_meta["item_id"] = assign_input.item_id
            task_meta["res_id"] = assign_input.robot_id
            task_meta["robot_id"] = assign_input.robot_id
            task_meta["assignment_status"] = "allocated"

        self._robots.setdefault(assign_input.robot_id, {})
        self._robots[assign_input.robot_id].update(
            {
                "robot_id": assign_input.robot_id,
                "task_id": assign_input.task_id,
                "item_id": assign_input.item_id,
                "status": "allocated",
            }
        )
        logger.info(
            "[MockStateManager] update_task_allocation: task=%s item=%s robot=%s",
            assign_input.task_id,
            assign_input.item_id,
            assign_input.robot_id,
        )

    async def update_task_status(self, req: UpdateTaskStatusInput) -> bool:
        task_key = req.task_id if req.task_id.startswith("task_") else f"task_{req.task_id}"
        task_meta = self._tasks.get(task_key)
        if task_meta is not None:
            task_meta["status"] = req.new_stat.value
            if req.error_code is not None:
                task_meta["error_code"] = req.error_code
        logger.info(
            "[MockStateManager] update_task_status: task=%s status=%s",
            req.task_id,
            req.new_stat.value,
        )
        if req.new_stat == TxnStat.SUCC and self._event_bridge is not None and task_meta is not None:
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
                        "task_type": task_meta.get("task_type"),
                    },
                )
            )
        return True

    async def publish_subtask_completed(
        self,
        *,
        task_id: str,
        item_id: int | None,
        subtask: str,
        task_type: str | None = None,
    ) -> bool:
        if self._event_bridge is None:
            return False

        task_key = task_id if task_id.startswith("task_") else f"task_{task_id}"
        task_meta = self._tasks.get(task_key, {})
        self._event_bridge.publish(
            Event(
                event_type=EventType.SUBTASK_COMPLETED,
                txn_id=task_meta.get("txn_id"),
                ord_id=task_meta.get("ord_id"),
                item_id=item_id if item_id is not None else task_meta.get("item_id"),
                res_id=task_meta.get("res_id"),
                payload={
                    "task_id": task_id,
                    "subtask": subtask,
                    "task_type": task_type or task_meta.get("task_type"),
                },
            )
        )
        logger.info(
            "[MockStateManager] publish_subtask_completed: task=%s subtask=%s item_id=%s",
            task_id,
            subtask,
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

        robot_meta = self._robots.setdefault(res_id, {})
        robot_meta["robot_id"] = res_id
        robot_meta["status"] = "idle"
        if task_id is not None:
            robot_meta["task_id"] = task_id
        if item_id is not None:
            robot_meta["item_id"] = item_id

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

    def mark_task_started(self, task_id: str, robot_id: str, is_trans: bool) -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["status"] = "PROC"
            self._tasks[task_id]["robot_id"] = robot_id
        logger.info("[MockStateManager] mark_task_started: task=%s robot=%s", task_id, robot_id)

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

    def update_robot_status_memory(self, robot_id: str, x: float, y: float, battery_pct: int) -> None:
        logger.debug(
            "[MockStateManager] update_robot_status_memory: robot=%s x=%s y=%s battery=%s",
            robot_id,
            x,
            y,
            battery_pct,
        )

    def update_amr_runtime_memory(
        self,
        robot_id: str,
        *,
        x: float | None = None,
        y: float | None = None,
        battery_pct: int | None = None,
    ) -> None:
        logger.debug(
            "[MockStateManager] update_amr_runtime_memory: robot=%s x=%s y=%s battery=%s",
            robot_id,
            x,
            y,
            battery_pct,
        )

    def update_robot_task_state(self, task_id: str, robot_id: str, cur_stat: str) -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["status"] = cur_stat
            self._tasks[task_id]["robot_id"] = robot_id
        logger.info(
            "[MockStateManager] update_robot_task_state: task=%s robot=%s cur_stat=%s",
            task_id,
            robot_id,
            cur_stat,
        )
