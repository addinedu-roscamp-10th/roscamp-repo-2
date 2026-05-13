"""Minimal in-memory StateManager stub for the refactor phase.

This module keeps orchestration state in memory and optionally mirrors runtime
state changes through a persistence repository. SQLAlchemy/schema mapping stays
outside this module.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ..contracts.enums import EventType
from ..contracts.enums import TaskType, TxnStat
from ..contracts.models import (
    AmrRuntimeState,
    AllocateTaskResInput,
    AdapterStepResultInput,
    CreateTaskInput,
    Event,
    ItemStatusRecord,
    StartProductionOrderAckModel,
    UpdateTaskStatusInput,
)

logger = logging.getLogger(__name__)
ITEM_AFFINITY_PREDECESSORS = {"MM", "POUR", "PP", "ToINSP", "INSP"}


def _parse_iso(value: Any) -> datetime | None:
    """ISO 8601 문자열 또는 UNIX timestamp 를 naive UTC datetime 으로 변환."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(value))
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                return datetime.utcfromtimestamp(float(value))
            except (TypeError, ValueError):
                return None
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def _normalize_task_type(task_type: Any) -> TaskType | None:
    if isinstance(task_type, TaskType):
        return task_type
    if isinstance(task_type, str):
        try:
            return TaskType(task_type)
        except ValueError:
            return None
    return None

def _storage_loc_tuple(raw: Any | None) -> tuple[int, int] | str | None:
    if raw is None:
        return None
    if isinstance(raw, tuple) and len(raw) == 2:
        try:
            return (int(raw[0]), int(raw[1]))
        except (TypeError, ValueError):
            return None
    return raw


class MockStateManager:
    """StateManager stub that only acknowledges production-start requests."""

    def __init__(self, event_bridge=None, task_manager=None,repository=None, enable_persistence: bool = False) -> None:
        self._items: dict[int, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._res_list: dict[str, dict[str, Any]] = {}
        self.adapter_results: list[dict[str, Any]] = []
        self.items = self._items
        self.orders: dict[int, dict[str, Any]] = {}

        self._event_bridge = event_bridge
        self._next_item_id = 1000
        self._next_equip_task_txn_id = 2000
        self._db_ready = False
        self._repo = repository
        self.task_manager = task_manager
        # INSP task payload 빌드 시 사용할 검사 이미지 정보 (item_id → payload dict).
        # UploadInspectionImage RPC 가 publish 한 INSP_IMAGE_UPLOADED 이벤트를
        # container.insp_image_responder 가 update_inspection_image() 로 저장하고,
        # orchestrator._build_execute_payload(INSP) 가 consume_inspection_image() 로
        # 1회 소비한다 (stale 방지).
        self._pending_inspection_images: dict[int, dict[str, Any]] = {}
        self._seed_res_pool()
        if self._repo is None and enable_persistence:
            try:
                from services.persistence.runtime_state_repository import RuntimeStateRepository

                self._repo = RuntimeStateRepository.from_default_db()
                self._db_ready = True
                logger.info("[MockStateManager] DB persistence enabled")
            except Exception as exc:
                logger.warning("[MockStateManager] DB persistence unavailable: %s", exc)
        elif self._repo is not None:
            self._db_ready = True
        logger.info("[MockStateManager] stub mode enabled")

    # 개발용 res seed
    def _seed_res_pool(self) -> None:
        seed_resources = [
            ("TAT1", "TAT", 0.0, 0.0, 80, "idle", None),
            ("TAT2", "TAT", 0.0, 0.0, 65, "ALLOC", None),
            ("TAT3", "TAT", 0.0, 0.0, 50, "idle", None),
            ("CONV1", "CONV", 0.0, 0.0, 100, "idle", None),
            ("PAT", "PAT", 0.0, 0.0, 100, "idle", None),
            ("MAT", "MAT", 0.0, 0.0, 100, "idle", None),
        ]
        for res_id, res_type, x, y, battery_pct, status, item_id in seed_resources:
            self._res_list[res_id] = {
                "res_id": res_id,
                "res_type": res_type,
                "task_id": None,
                "item_id": item_id,
                "status": status,
                "x": x,
                "y": y,
                "battery_pct": battery_pct,
                "condition": "NORMAL",
            }
        # 충전소 상태 정보 (메모리 관리용)
        self.charger_slots: dict[tuple[int, int], dict[str, Any]] = {
            (1, 1): {"status": "empty", "res_id": None},
            (1, 2): {"status": "empty", "res_id": None},
            (1, 3): {"status": "empty", "res_id": None},
        }

    def _safe_repo_call(self, method_name: str, *args, **kwargs):
        if not self._db_ready or self._repo is None:
            return None
        method = getattr(self._repo, method_name, None)
        if method is None:
            return None
        try:
            return method(*args, **kwargs)
        except Exception:
            logger.exception("[MockStateManager] persistence %s failed", method_name)
            return None

    async def get_order_target_qty(self, ord_id: int) -> int | None:
        repo_qty = self._safe_repo_call("get_order_target_qty", ord_id)
        if repo_qty is not None:
            normalized_qty = max(int(repo_qty), 1)
            self.orders.setdefault(ord_id, {})["target"] = normalized_qty
            return normalized_qty

        order_meta = self.orders.get(ord_id, {})
        target_qty = order_meta.get("target")
        if target_qty is None:
            return None
        return max(int(target_qty), 1)

    def reserve_storage_slots(
        self,
        start_pos: tuple[int, int],
        target_qty: int,
    ) -> int | None:
        return self._safe_repo_call(
            "reserve_storage_slots",
            int(start_pos[0]),
            int(start_pos[1]),
            int(target_qty),
        )

    async def get_empty_charger(self, res_id: str | None = None) -> tuple[int, int] | None:
        """충전 가능한 슬롯을 반환하고, 최초 조회 시 즉시 예약한다."""
        if res_id is not None:
            for chg_loc, slot_info in self.charger_slots.items():
                if slot_info.get("res_id") == res_id:
                    return chg_loc

        for chg_loc, slot_info in self.charger_slots.items():
            if slot_info.get("status") == "empty":
                reserved = self._safe_repo_call(
                    "reserve_charger_slot",
                    int(chg_loc[0]),
                    int(chg_loc[1]),
                )
                if reserved is False:
                    continue
                slot_info["status"] = "reserved"
                slot_info["res_id"] = res_id
                logger.info(
                    "[MockStateManager] Charger reserved: chg_loc=%s res_id=%s",
                    chg_loc,
                    res_id,
                )
                return chg_loc

        logger.warning("[MockStateManager] No empty charger available for res_id=%s", res_id)
        return None

    def _release_charger(self, res_id: str | None = None) -> None:
        """충전 완료된 AMR의 슬롯 예약을 해제한다."""
        if res_id is None:
            return

        for chg_loc, slot_info in self.charger_slots.items():
            if slot_info.get("res_id") != res_id:
                continue
            self._safe_repo_call(
                "release_charger_slot",
                int(chg_loc[0]),
                int(chg_loc[1]),
                res_id,
            )
            slot_info["status"] = "empty"
            slot_info["res_id"] = None
            logger.info(
                "[MockStateManager] Charger released: chg_loc=%s res_id=%s",
                chg_loc,
                res_id,
            )
            return

    def _occupy_charger(self, res_id: str | None = None) -> None:
        """예약된 충전 슬롯을 AMR 점유 상태로 확정한다."""
        if res_id is None:
            return

        for chg_loc, slot_info in self.charger_slots.items():
            if slot_info.get("res_id") != res_id:
                continue
            slot_info["status"] = "occupied"
            self._safe_repo_call(
                "occupy_charger_slot",
                res_id,
                int(chg_loc[0]),
                int(chg_loc[1]),
            )
            logger.info(
                "[MockStateManager] Charger occupied: chg_loc=%s res_id=%s",
                chg_loc,
                res_id,
            )
            return

    # 4. 트랜잭션 실패(Rollback) 처리를 위한 txn 업데이트 요청 함수
    async def update_txn_fail_for_rollback(self, item_id: int) -> None:
        """스매에게 txn업데이트요청('fail') - 시나리오 6단계 반영"""
        # 해당 아이템의 flow_stat을 CAST로 원복 (재시작 준비)
        if item_id in self._items:
            self._items[item_id]["flow_stat"] = "CAST"
            logger.info("[MockStateManager] Item %s status rolled back to CAST due to battery low", item_id)


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

        order_meta = self.orders.get(ord_id, {})
        target_qty = max(int(order_meta.get("target", 1) or 1), 1)
        ptn_id = order_meta.get("ptn_loc_id") or order_meta.get("ptn_id")
        rack_start_pos = order_meta.get("rack_start_pos")

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
                "ptn_id": ptn_id,
                "strg_loc": None,
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

            equip_task_txn_ids.append(equip_task_txn_id)

        return StartProductionOrderAckModel(
            ord_id=ord_id,
            accepted=True,
            reason=f"Accepted by MockStateManager. created_items={len(item_ids)}",
            item_ids=item_ids,
            equip_task_txn_ids=[],
        )

    def _start_production_db(self, ord_id: int) -> StartProductionOrderAckModel:
        if self._repo is None:
            return StartProductionOrderAckModel(
                ord_id=ord_id,
                accepted=False,
                reason="DB-backed start_production is not fully initialized.",
            )

        ack = self._repo.start_production(ord_id)
        if not ack.accepted:
            return ack

        ptn_id = self._safe_repo_call("get_order_ptn_loc_id", ord_id)
        for item_id, txn_id in zip(ack.item_ids, ack.equip_task_txn_ids):
            self._items[item_id] = {
                "item_id": item_id,
                "ord_id": ord_id,
                "order_id": ord_id,
                "flow_stat": "CREATED",
                "zone_nm": "PAT",
                "result": None,
                "ptn_id": ptn_id,
            }
            self._tasks[f"task_{txn_id}"] = {
                "ord_id": ord_id,
                "item_id": item_id,
                "txn_id": txn_id,
                "status": TxnStat.QUE.value,
                "res_id": "PAT",
                "task_type": TaskType.MM.value,
            }
        return ack

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
            strg_loc=_storage_loc_tuple(item.get("strg_loc")),
        )

    async def get_items_by_order(self, ord_id: int) -> list[ItemStatusRecord]:
        """특정 주문의 모든 아이템 정보를 조회."""
        result = []
        for item_id, item_meta in self._items.items():
            item_order_id = int(item_meta.get("order_id") or item_meta.get("ord_id") or 0)
            if item_order_id == ord_id:
                result.append(await self.get_item(item_id))
        return result

    async def insert_task_txn(self, task_input: CreateTaskInput) -> int:
        txn_id = self._next_equip_task_txn_id
        self._next_equip_task_txn_id += 1
        task_id = f"task_{txn_id}"
        item_meta = self._items.setdefault(task_input.item_id, {"item_id": task_input.item_id})
        task_meta = {
            "txn_id": txn_id,
            "item_id": task_input.item_id,
            "ord_id": item_meta.get("ord_id") or item_meta.get("order_id"),
            "task_type": task_input.task_type.value,
            "status": str(task_input.txn_stat),
            "res_id": task_input.res_id,
        }
        db_txn_id = self._safe_repo_call("sync_task_created", task_meta)
        if db_txn_id is not None:
            txn_id = int(db_txn_id)
            self._next_equip_task_txn_id = max(self._next_equip_task_txn_id, txn_id + 1)
            task_id = f"task_{txn_id}"
            task_meta["txn_id"] = txn_id
        self._tasks[task_id] = task_meta
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

    async def get_available_resources(self, req_res_type: str) -> list[str]:
        available = [
            res_id
            for res_id, res_meta in sorted(self._res_list.items())
            if res_meta.get("res_type") == req_res_type
            and res_meta.get("status") in {"idle", "toidle"}
            and res_meta.get("item_id") is None
        ]
        return available

    async def get_amr_stats(self) -> list[AmrRuntimeState]:
        return [
            AmrRuntimeState(
                res_id=res_id,
                x=float(res_meta.get("x", 0.0)),
                y=float(res_meta.get("y", 0.0)),
                bat_pct=int(res_meta["battery_pct"]),
            )
            for res_id, res_meta in sorted(self._res_list.items())
            if res_meta.get("res_type") == "TAT"
        ]

    def is_res_available(self, res_id: str) -> bool:
        res_meta = self._res_list.get(res_id)
        if res_meta is None:
            return False
        return res_meta.get("status") in {"idle", "toidle"} and res_meta.get("item_id") is None

    def update_amr_charger_return_state(
        self,
        res_id: str,
        status: str,
        source: str | None = None,
    ) -> None:
        """Task가 아닌 충전소 복귀 후처리 상태를 반영한다."""
        res_meta = self._res_list.setdefault(res_id, {"res_id": res_id})
        res_meta.update(
            {
                "res_id": res_id,
                "task_id": None,
                "item_id": None,
                "status": status,
                "task_type": "charger_return",
            }
        )
        if source is not None:
            res_meta["charger_return_source"] = source
        if status in {"idle", "charging"}:
            self._occupy_charger(res_id)
        self._safe_repo_call("sync_resource_snapshot", res_meta)
        logger.info(
            "[MockStateManager] update_amr_charger_return_state: res=%s status=%s source=%s",
            res_id,
            status,
            source,
        )

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
            task_meta["task_id"] = task_key

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
        if task_meta is not None:
            self._safe_repo_call("sync_task_status", task_meta)
        self._safe_repo_call("sync_resource_snapshot", self._res_list[assign_input.res_id])
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
            task_meta["task_id"] = task_key
            task_meta["status"] = req.new_stat.value
            if req.error_code is not None:
                task_meta["error_code"] = req.error_code
            if req.new_stat == TxnStat.PROC and assigned_res_id is not None:
                self._handle_proc_task_status(req, task_meta, assigned_res_id)
            if req.new_stat == TxnStat.SUCC:
                suppress_task_completed = self._handle_success_task_status(
                    task_meta,
                    previous_status,
                )
            if req.new_stat in {TxnStat.SUCC, TxnStat.FAIL} and assigned_res_id is not None:
                should_continue, suppress_resource_available = self._handle_terminal_resource_status(
                    req,
                    task_meta,
                    assigned_res_id,
                )
                if not should_continue:
                    return True
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
        if task_meta is not None:
            self._safe_repo_call("sync_task_status", task_meta)
        return True

    async def record_adapter_result(self, req: AdapterStepResultInput) -> bool:
        """Adapter 결과를 메모리에 기록하고, 가능하면 DB 로그로 mirror한다."""
        task_key = req.task_id if req.task_id.startswith("task_") else f"task_{req.task_id}"
        task_meta = self._tasks.get(task_key, {})
        record = req.model_dump()
        record["txn_id"] = task_meta.get("txn_id")
        record["ord_id"] = task_meta.get("ord_id")
        self.adapter_results.append(record)
        self._safe_repo_call("record_adapter_result", record)
        logger.info(
            "[MockStateManager] record_adapter_result: task=%s step=%s action=%s success=%s",
            req.task_id,
            req.step_id,
            req.action,
            req.success,
        )
        return True

    def _handle_proc_task_status(
        self,
        req: UpdateTaskStatusInput,
        task_meta: dict[str, Any],
        assigned_res_id: str,
    ) -> None:
        """Task 시작 시 따라오는 상태 반영과 이벤트 처리"""
        current_task_type = _normalize_task_type(task_meta.get("task_type"))
        if current_task_type != TaskType.ToCHG:
            self._release_charger(assigned_res_id)

        item_id = task_meta.get("item_id")
        if item_id is not None and task_meta.get("task_type") == "MM":
            item = self._items.setdefault(item_id, {"item_id": item_id})
            if item.get("flow_stat") == "CREATED":
                item["flow_stat"] = "CAST"
            item["last_task_type"] = task_meta.get("task_type")
            item["req_res_id"] = task_meta.get("res_id")
            self._safe_repo_call("sync_item_snapshot", item)

        res_meta = self._res_list.setdefault(assigned_res_id, {"res_id": assigned_res_id})
        is_battery_low_tochg = self._is_battery_low_tochg(current_task_type, res_meta)
        if current_task_type == TaskType.ToCHG:
            status = "charging" if is_battery_low_tochg else "toidle"
            res_meta.update(
                {
                    "task_id": req.task_id,
                    "item_id": None,
                    "status": status,
                    "task_type": task_meta.get("task_type"),
                }
            )
        else:
            res_meta.update(
                {
                    "task_id": req.task_id,
                    "item_id": task_meta.get("item_id"),
                    "status": TxnStat.PROC.value,
                    "task_type": task_meta.get("task_type"),
                }
            )
        self._safe_repo_call("sync_resource_snapshot", res_meta)

        if (
            current_task_type == TaskType.ToCHG
            and self._event_bridge is not None
            and isinstance(res_meta.get("res_type"), str)
            and res_meta.get("res_type")
            and not is_battery_low_tochg
        ):
            self._event_bridge.publish(
                Event(
                    event_type=EventType.RESOURCE_AVAILABLE,
                    item_id=task_meta.get("item_id"),
                    res_id=assigned_res_id,
                    payload={
                        "req_res_type": res_meta.get("res_type"),
                        "task_id": req.task_id,
                        "status": "toidle",
                    },
                )
            )

    def _handle_success_task_status(
        self,
        task_meta: dict[str, Any],
        previous_status: str | None,
    ) -> bool:
        """Task 성공 시 따라오는 상태 반영과 이벤트 처리"""
        suppress_task_completed = task_meta.get("task_type") == "ToSTRG"
        item_id = task_meta.get("item_id")
        if item_id is not None:
            item = self._items.setdefault(item_id, {"item_id": item_id})
            item["last_task_type"] = task_meta.get("task_type")
            item["req_res_id"] = (
                task_meta.get("res_id")
                if task_meta.get("task_type") in ITEM_AFFINITY_PREDECESSORS
                else None
            )
            self._safe_repo_call("sync_item_snapshot", item)
        if previous_status != TxnStat.SUCC.value and task_meta.get("task_type") == "PA_GP":
            suppress_task_completed = self._handle_pa_gp_completion(task_meta)
        return suppress_task_completed

    def _handle_terminal_resource_status(
        self,
        req: UpdateTaskStatusInput,
        task_meta: dict[str, Any],
        assigned_res_id: str,
    ) -> tuple[bool, bool]:
        """Task 완료/실패 시 resource 상태를 갱신하고 RESOURCE_AVAILABLE 발행 여부를 결정한다."""
        suppress_resource_available = False
        res_meta = self._res_list.setdefault(assigned_res_id, {"res_id": assigned_res_id})
        current_task_id = res_meta.get("task_id")
        if current_task_id not in {None, req.task_id}:
            self._safe_repo_call("sync_task_status", task_meta)
            logger.info(
                "[MockStateManager] resource reset skipped: task=%s res=%s current_task=%s",
                req.task_id,
                assigned_res_id,
                current_task_id,
            )
            return False, suppress_resource_available

        current_task_type = _normalize_task_type(task_meta.get("task_type"))
        is_battery_low_tochg = self._is_battery_low_tochg(current_task_type, res_meta)
        if is_battery_low_tochg:
            self._occupy_charger(assigned_res_id)
            res_meta.update(
                {
                    "task_id": None,
                    "item_id": None,
                    "status": "charging",
                    "task_type": task_meta.get("task_type"),
                }
            )
            self._safe_repo_call("sync_resource_snapshot", res_meta)
            return True, True

        keep_item_affinity = (
            req.new_stat == TxnStat.SUCC
            and task_meta.get("task_type") in ITEM_AFFINITY_PREDECESSORS
        )
        res_meta.update(
            {
                "task_id": None,
                "status": "idle",
                "task_type": task_meta.get("task_type"),
            }
        )
        if keep_item_affinity:
            res_meta["item_id"] = task_meta.get("item_id")
            suppress_resource_available = True
        else:
            res_meta["item_id"] = None
        self._safe_repo_call("sync_resource_snapshot", res_meta)

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
        return True, suppress_resource_available

    def _is_battery_low_tochg(
        self,
        task_type: TaskType | None,
        res_meta: dict[str, Any],
    ) -> bool:
        return task_type == TaskType.ToCHG and res_meta.get("condition") == "BATTERY_LOW"

    def _handle_pa_gp_completion(self, task_meta: dict[str, Any]) -> bool:
        """PA_GP 성공 시 DB 적재 슬롯을 occupied로 확정하고, gp_qty를 증가시켜 주문 생산 완료 여부를 판단."""
        ord_id = task_meta.get("ord_id")
        if ord_id is None:
            return False

        item_id = task_meta.get("item_id")
        if item_id is not None:
            item = self._items.get(item_id, {})
            strg_loc = _storage_loc_tuple(item.get("strg_loc"))
            if isinstance(strg_loc, tuple):
                self._safe_repo_call(
                    "update_item_storage_location",
                    item_id,
                    int(strg_loc[0]),
                    int(strg_loc[1]),
                )

        db_complete = self._safe_repo_call("increment_order_gp_qty", ord_id)
        if db_complete is not None:
            if isinstance(db_complete, dict):
                complete = bool(db_complete.get("complete"))
                gp_qty = db_complete.get("gp_qty")
                target_qty = db_complete.get("qty")
                logger.info(
                    "[MockStateManager] PA_GP completion: ord_id=%s gp_qty=%s qty=%s complete=%s",
                    ord_id,
                    gp_qty,
                    target_qty,
                    complete,
                )
                if complete:
                    logger.info(
                        "[MockStateManager] ord_id=%s qty=%s 생산완료",
                        ord_id,
                        target_qty,
                    )
                    self.task_manager.log_slot_table()

                    if self.task_manager is not None:  
                        self.task_manager.remove_order_reserve(ord_id)  
                        self.task_manager.remove_order_config(ord_id)  
                        #self.task_manager.log_slot_table()
                    else:  
                        logger.warning("task_manager is not configured; order cleanup skipped")  

                return complete

            logger.info(
                "[MockStateManager] PA_GP completion: ord_id=%s complete=%s",
                ord_id,
                db_complete,
            )
            if db_complete:
                logger.info("[MockStateManager] ord_id=%s 생산완료", ord_id)

                if self.task_manager is not None:  
                    self.task_manager.remove_order_reserve(ord_id) 
                    self.task_manager.remove_order_config(ord_id)
                    #self.task_manager.log_slot_table()  
                else:  
                    logger.warning("task_manager is not configured; order cleanup skipped")  


            return bool(db_complete)

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

            if self.task_manager is not None:  
                self.task_manager.remove_order_reserve(ord_id)  
                self.task_manager.remove_order_config(ord_id)
                #self.task_manager.log_slot_table()  
            else: 
                logger.warning("task_manager is not configured; order cleanup skipped")  


        return is_complete

    def update_inspection_image(
        self,
        item_id: int,
        payload: dict[str, Any],
    ) -> None:
        """INSP_IMAGE_UPLOADED 시 image_path 등 검사 이미지 정보 저장.

        UploadInspectionImage RPC 가 발행한 INSP_IMAGE_UPLOADED 이벤트를
        container.insp_image_responder 가 받아 본 메서드로 위임.
        같은 item_id 가 재호출되면 마지막 payload 로 덮어쓴다 (재캡처 케이스).
        """
        if item_id <= 0:
            return
        self._pending_inspection_images[int(item_id)] = dict(payload or {})
        logger.info(
            "[MockStateManager] update_inspection_image: item_id=%s keys=%s",
            item_id,
            sorted(self._pending_inspection_images[int(item_id)].keys()),
        )

    def consume_inspection_image(
        self,
        item_id: int,
    ) -> dict[str, Any] | None:
        """INSP task payload 빌드 시 사용. 1회 소비 후 clear (stale 방지)."""
        if item_id <= 0:
            return None
        return self._pending_inspection_images.pop(int(item_id), None)

    async def record_inspection_result(
        self,
        *,
        item_id: int,
        inference: dict[str, Any] | None,
    ) -> bool:
        """INSP task 의 AI_INFERENCE_REQUEST step 결과를 받아 DB 4-table 갱신.

        task_executor 가 AIAdapter step 종료 직후 호출. AIAdapter 는 DB 를 건드리지
        않으므로 본 메서드가 단일 ownership 으로 영속화 책임을 수행한다.

        성공 시: insp_task_txn SUCC + ai_inference_txn / insp_stat INSERT + item.is_defective.
        실패 시 (inference.ok=False 또는 예외): insp_task_txn FAIL.

        Returns:
            True 면 4-table 갱신 성공, False 면 실패 (FAIL row 까지 기록 완료).
        """
        if item_id is None or int(item_id) <= 0:
            logger.warning(
                "[MockStateManager] record_inspection_result: invalid item_id=%s — skip",
                item_id,
            )
            return False
        item_id_int = int(item_id)
        inference = inference or {}

        try:
            from services.command.inspection_result_command import (
                InspectionResultCommand,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MockStateManager] record_inspection_result: import 실패 item_id=%d exc=%s",
                item_id_int,
                exc,
            )
            return False

        cmd = InspectionResultCommand()

        if not inference.get("ok"):
            reason = inference.get("error_reason") or "ai_inference_failed"
            try:
                cmd.record_inspection_failure(item_id=item_id_int, reason=reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[MockStateManager] record_inspection_failure 실패 item_id=%d exc=%s",
                    item_id_int,
                    exc,
                )
            return False

        try:
            recorded = cmd.record_inspection_result(
                item_id=item_id_int,
                is_defective=bool(inference.get("is_defective")),
                predicted_class=inference.get("predicted_class"),
                yolo_confidence=inference.get("yolo_confidence"),
                anomaly_score=inference.get("anomaly_score"),
                anomaly_threshold=inference.get("anomaly_threshold"),
                model_id=inference.get("model_id"),
                model_type=inference.get("model_type"),
                step_type=inference.get("step_type"),
                started_at=_parse_iso(inference.get("started_at")),
                completed_at=_parse_iso(inference.get("completed_at")),
                raw_inference_payload=inference.get("raw_payload"),
            )
            logger.info(
                "[MockStateManager] record_inspection_result OK: item_id=%d insp_txn=%d "
                "result=%s class=%s",
                item_id_int,
                recorded.insp_txn_id,
                recorded.result,
                recorded.predicted_class or "<none>",
            )
            return True
        except (LookupError, ValueError) as exc:
            logger.warning(
                "[MockStateManager] record_inspection_result 실패 item_id=%d exc=%s — FAIL 기록 시도",
                item_id_int,
                exc,
            )
            try:
                cmd.record_inspection_failure(item_id=item_id_int, reason=str(exc))
            except Exception as exc2:  # noqa: BLE001
                logger.warning(
                    "[MockStateManager] record_inspection_failure (after persist err) 실패 "
                    "item_id=%d exc=%s",
                    item_id_int,
                    exc2,
                )
            return False

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
        res_meta = self._res_list.setdefault(res_id, {})
        res_meta["res_id"] = res_id
        res_meta["status"] = "idle"
        if task_id is not None:
            res_meta["task_id"] = task_id
        if item_id is not None:
            res_meta["item_id"] = item_id
        self._safe_repo_call("sync_resource_snapshot", res_meta)
        self._release_charger(res_id)

        if self._event_bridge is None:
            return False

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
            self._tasks[task_id]["task_id"] = task_id
            self._safe_repo_call("sync_task_status", self._tasks[task_id])
        res_meta = self._res_list.setdefault(res_id, {"res_id": res_id})
        res_meta.update({"task_id": task_id, "status": "PROC"})
        self._safe_repo_call("sync_resource_snapshot", res_meta)
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
        self._safe_repo_call("sync_item_snapshot", item)
        logger.info(
            "[MockStateManager] update_item_status: item=%s flow_stat=%s zone_nm=%s",
            item_id,
            flow_stat,
            zone_nm,
        )

    def update_amr_runtime_memory(
        self,
        res_id: str,
        *,
        x: float | None = None,
        y: float | None = None,
        battery_pct: int | None = None,
    ) -> None:
        res_meta = self._res_list.setdefault(res_id, {"res_id": res_id})
        if x is not None:
            res_meta["x"] = x
        if y is not None:
            res_meta["y"] = y
        if battery_pct is not None:
            res_meta["battery_pct"] = battery_pct
        self._safe_repo_call("sync_resource_snapshot", res_meta)
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
            self._tasks[task_id]["task_id"] = task_id
            self._safe_repo_call("sync_task_status", self._tasks[task_id])
        res_meta = self._res_list.setdefault(res_id, {"res_id": res_id})
        res_meta.update(
            {
                "task_id": None if cur_stat in {TxnStat.SUCC.value, TxnStat.FAIL.value} else task_id,
                "status": "idle" if cur_stat in {TxnStat.SUCC.value, TxnStat.FAIL.value} else cur_stat,
            }
        )
        self._safe_repo_call("sync_resource_snapshot", res_meta)
        logger.info(
            "[MockStateManager] update_res_task_state: task=%s res=%s cur_stat=%s",
            task_id,
            res_id,
            cur_stat,
        )
    # 적재 예정 위치를 메모리에 기록한다. DB 점유 확정은 PA_GP 성공 시 처리한다.
    async def update_item_storage_location(self, item_id: int, strg_loc: tuple[int, int] | str) -> None:
        item = self._items.setdefault(item_id, {"item_id": item_id})
        item["strg_loc"] = _storage_loc_tuple(strg_loc)

        logger.info(
            "[MockStateManager] item storage location saved: item=%s strg_loc=%s",
            item_id,
            item["strg_loc"],
        )

    #주문 수량만큼 공간이 충분하면 가장 작은 위치를 반환
    async def get_empty_start_slot(self, target_qty: int) -> tuple[int, int] | None:
        """
        DB 기준으로 빈 적재 슬롯을 조회하고,
        주문 수량만큼 공간이 충분하면 가장 작은 위치를 반환한다.
        """
        db_slots = self._safe_repo_call("get_storage_slots")

        # DB 조회 실패 시 메모리로
        if db_slots is None:
            logger.warning("[MockStateManager] DB slot 조회 실패 또는 repo 없음")
            if self.task_manager is None:
                return None

            empty_slots = [
                (row, col)
                for (row, col), slot in sorted(self.task_manager.slot_table.items())
                if slot.get("status") == "Empty"
                and slot.get("order_id") is None
            ]
        # DB 조회
        else:
            empty_slots = [
                (int(slot["row"]), int(slot["col"]))
                for slot in db_slots
                if slot.get("status") == "Empty"
                and slot.get("order_id") is None
            ]

        empty_slots.sort()

        if len(empty_slots) < target_qty:
            return None

        row, col = empty_slots[0]
        return (row, col)
