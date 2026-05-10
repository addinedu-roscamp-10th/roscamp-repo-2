"""Minimal in-memory StateManager stub for the refactor phase.

This module keeps orchestration state in memory and optionally mirrors runtime
state changes through a persistence repository. SQLAlchemy/schema mapping stays
outside this module.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any

from ..contracts.enums import EventType
from ..contracts.enums import TaskType, TxnStat
from ..contracts.models import (
    AmrRuntimeState,
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


def _strg_loc_id(strg_loc: Any | None) -> int | None:
    if strg_loc is None:
        return None
    try:
        return int(strg_loc)
    except (TypeError, ValueError):
        pass

    try:
        row_text, col_text = str(strg_loc).split("-", maxsplit=1)
        row = int(row_text)
        col = int(col_text)
    except (TypeError, ValueError):
        return None

    if row < 1 or col < 1 or col > 6:
        return None
    return (row - 1) * 6 + col


class MockStateManager:
    """StateManager stub that only acknowledges production-start requests."""

    def __init__(self, event_bridge=None, repository=None, enable_persistence: bool = False) -> None:
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
        self._repo = repository
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
            ("TAT1", "TAT", 0.0, 0.0, 80),
            ("TAT2", "TAT", 0.0, 0.0, 65),
            ("TAT3", "TAT", 0.0, 0.0, 50),
            ("CONV1", "CONV", 0.0, 0.0, 100),
            ("PAT", "RA_STRG", 0.0, 0.0, 100),
            ("MAT", "RA_CAST", 0.0, 0.0, 100),
        ]
        for res_id, res_type, x, y, battery_pct in seed_resources:
            self._res_list[res_id] = {
                "res_id": res_id,
                "res_type": res_type,
                "task_id": None,
                "item_id": None,
                "status": "idle",
                "x": x,
                "y": y,
                "battery_pct": battery_pct,
                "condition": "NORMAL",
            }
        # 충전소 상태 정보 (메모리 관리용)
        self.charger_slots = {'CHG_01': 'EMPTY', 'CHG_02': 'EMPTY'}

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

        # 충전소 상태 정보 (메모리 관리용)
        self.charger_slots = {'CHG_01': 'EMPTY', 'CHG_02': 'EMPTY'}

    
    async def force_battery_emergency_test(self, amr_id="TAT1", item_id=1005):
        """
        5초 대기 후, 배터리 방전 이벤트를 강제로 발행하여 
        오케스트레이터의 비상 복구 로직이 트리거되는지 테스트합니다.
        """
        logger.info(f"[Test] 비상 시뮬레이션 예약: 5초 뒤 {amr_id} 배터리 방전 이벤트 발생")
        
        # 1. 5초 대기
        await asyncio.sleep(5)
        arm_id = "MAT"
        
        # 2. 비상 이벤트 생성
        emergency_event = Event(
            event_type=EventType.AMR_BATTERY_LOW,
            item_id=item_id,
            payload={
                "battery_pct": 25,
                "condition": "BATTERY_LOW",
                "amr_id": amr_id,
                "arm_id": arm_id,
            }
        )
        
        # 3. 클래스 내부의 self._event_bridge를 사용하여 이벤트 발행
        if self._event_bridge:
            self._event_bridge.publish(emergency_event)
            logger.warning(f"[Test] 5초 경과: {amr_id} 배터리 방전 이벤트 퍼블리시 완료!")
        else:
            logger.error("[Test] 이벤트 브리지가 설정되지 않아 이벤트를 쏠 수 없습니다.")

# 2. 배터리 업데이트 및 이벤트 발행 로직
    async def update_amr_runtime_memory(
        self,
        res_id: str,
        *,
        x: float | None = None,
        y: float | None = None,
        battery_pct: int | None = None,
    ) -> None:
        """배터리 수신(1단계) 및 조건 만족 시 이벤트 발행(2단계)"""
        res_meta = self._res_list.get(res_id)
        if not res_meta:
            return

        if x is not None: res_meta["x"] = x
        if y is not None: res_meta["y"] = y
        
        if battery_pct is not None:
            res_meta["battery_pct"] = battery_pct
            
            # 방전 감지 (30% 미만) 및 중복 이벤트 방지(condition 체크)
            if battery_pct < 30 and res_meta.get("condition") == "NORMAL":
                res_meta["condition"] = "BATTERY_LOW"
                logger.warning("[MockStateManager] AMR %s 배터리 부족 감지 (%s%%)", res_id, battery_pct)
                
                #여기서 res_id 로 item_id 찾고 그 item_id 가지고 있는 arm res_id 찾기
                arm_id = "MAT"
                # 오케스트레이터가 구독 중인 이벤트 브리지로 발행 (2단계)
                if self._event_bridge:
                    self._event_bridge.publish(
                        Event(
                            event_type=EventType.AMR_BATTERY_LOW, # Enums에 해당 타입이 있다고 가정
                            item_id=res_meta.get("item_id"),      # 현재 물고 있는 아이템 ID
                            amr_id=res_id,
                            arm_id=arm_id,
                            payload={
                                "battery_pct": battery_pct,
                                "condition": "BATTERY_LOW"
                            }
                        )
                    )

    # 3. 빈 충전소 찾기 메서드 추가
    def get_empty_charger(self) -> str | None:
        """충전 가능한 슬롯 반환"""
        """for slot, state in self.charger_slots.items():
            if state == 'EMPTY':
                return slot
        return None"""
        #임시로
        return "1-1"

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
            strg_loc=item.get("strg_loc")
        )

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
            #"strg_loc": task_input.strg_loc,
            #"strg_loc_id": _strg_loc_id(task_input.strg_loc),
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
                item_id = task_meta.get("item_id")
                if item_id is not None and task_meta.get("task_type") == "MM":
                    item = self._items.setdefault(item_id, {"item_id": item_id})
                    if item.get("flow_stat") == "CREATED":
                        item["flow_stat"] = "CAST"
                    item["last_task_type"] = task_meta.get("task_type")
                    item["req_res_id"] = task_meta.get("res_id")
                    self._safe_repo_call("sync_item_snapshot", item)
                res_meta = self._res_list.setdefault(assigned_res_id, {"res_id": assigned_res_id})
                res_meta.update(
                    {
                        "task_id": req.task_id,
                        "item_id": task_meta.get("item_id"),
                        "status": TxnStat.PROC.value,
                        "task_type": task_meta.get("task_type"),
                    }
                )
                self._safe_repo_call("sync_resource_snapshot", res_meta)
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
                    self._safe_repo_call("sync_item_snapshot", item)
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

    def _handle_pa_gp_completion(self, task_meta: dict[str, Any]) -> bool:
        ord_id = task_meta.get("ord_id")
        if ord_id is None:
            return False

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
                return complete

            logger.info(
                "[MockStateManager] PA_GP completion: ord_id=%s complete=%s",
                ord_id,
                db_complete,
            )
            if db_complete:
                logger.info("[MockStateManager] ord_id=%s 생산완료", ord_id)
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
        self._safe_repo_call("sync_resource_snapshot", res_meta)

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

    def update_res_status_memory(self, res_id: str, x: float, y: float, battery_pct: int) -> None:
        res_meta = self._res_list.setdefault(res_id, {"res_id": res_id})
        res_meta.update(
            {
                "x": x,
                "y": y,
                "battery_pct": battery_pct,
            }
        )
        self._safe_repo_call("sync_resource_snapshot", res_meta)
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
