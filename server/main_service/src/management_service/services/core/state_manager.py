"""Minimal in-memory StateManager stub for the refactor phase.

This module keeps orchestration state in memory and optionally mirrors runtime
state changes through a persistence repository. SQLAlchemy/schema mapping stays
outside this module.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Any

from ..contracts.enums import EventType
from ..contracts.enums import ITEM_AFFINITY_PREDECESSORS, FlowStatus, TaskType, TxnStat
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


def _make_task_key(task_id: Any, task_type: TaskType | str) -> str:
    # trans_task_txn / pp_task_txn / equip_task_txn / insp_task_txn 가 PK 시퀀스를 독립으로
    # 가져 같은 정수 id 가 여러 테이블에 동시에 존재할 수 있으므로 task_type 을 prefix 로
    # 붙여 메모리 키 충돌을 막는다.
    raw = str(task_id)
    for prefix in (t.value + ":" for t in TaskType):
        if raw.startswith(prefix):
            return raw
    if isinstance(task_type, TaskType):
        return f"{task_type.value}:{raw}"
    if isinstance(task_type, str) and task_type:
        return f"{task_type}:{raw}"
    raise ValueError("task_type is required to build a task key")


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


def _storage_loc_tuple(raw: Any | None) -> tuple[int, int] | None:
    if raw is None:
        return None
    if isinstance(raw, tuple) and len(raw) == 2:
        try:
            return (int(raw[0]), int(raw[1]))
        except (TypeError, ValueError):
            return None
    return None


class StateManager:
    """memory + DB 상태 저장소."""

    def __init__(self, event_bridge=None, task_manager=None,repository=None, enable_persistence: bool = False) -> None:
        self._items: dict[int, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._res_list: dict[str, dict[str, Any]] = {}
        self.items = self._items
        self.orders: dict[int, dict[str, Any]] = {}

        self._event_bridge = event_bridge
        self._next_item_id = 1000
        self._next_equip_task_txn_id = 2000
        self._db_ready = False
        self._repo = repository
        self.task_manager = task_manager
        self.adapter_results: list[dict[str, Any]] = []
        # INSP task payload 빌드 시 사용할 검사 이미지 정보 (item_id → payload dict).
        # UploadInspectionImage RPC 가 publish 한 INSP_IMAGE_UPLOADED 이벤트를
        # container.insp_image_responder 가 update_inspection_image() 로 저장하고,
        # orchestrator._build_execute_payload(INSP) 가 consume_inspection_image() 로
        # 1회 소비한다 (stale 방지).
        self._pending_inspection_images: dict[int, dict[str, Any]] = {}
        self._charger_lock = asyncio.Lock()
        if self._repo is None and enable_persistence:
            try:
                from services.persistence.runtime_state_repository import RuntimeStateRepository

                self._repo = RuntimeStateRepository.from_default_db()
                self._db_ready = True
                logger.info("[StateManager] DB persistence enabled")
            except Exception as exc:
                logger.warning("[StateManager] DB persistence unavailable: %s", exc)
        elif self._repo is not None:
            self._db_ready = True
        # _seed_res_pool은 DB 초기화 이후에 호출 — 사용 가능 시 DB master를 우선 사용.
        self._seed_res_pool()
        logger.info("[StateManager] stub mode enabled")

    # 자원/충전소: 시나리오 JSON에서 로드 (mock 시작 상태)
    # transfer_points / battery_thresholds: DB master 우선, 없으면 JSON 폴백
    def _seed_res_pool(self) -> None:
        from services.seed import load_scenario
        seed = load_scenario()

        self._res_list.clear()
        for r in seed["resources"]:
            self._res_list[r["res_id"]] = {
                "res_id": r["res_id"],
                "res_type": r["res_type"],
                "task_id": None,
                "item_id": None,
                "status": r.get("status", "IDLE"),
                "x": float(r.get("x", 0.0)),
                "y": float(r.get("y", 0.0)),
                "battery_pct": int(r.get("battery_pct", 100)),
            }

        self.charger_slots: dict[tuple[int, int], dict[str, Any]] = {
            (int(s["row"]), int(s["col"])): {
                "status": s.get("status", "empty"),
                "res_id": s.get("res_id"),
            }
            for s in seed["charger_slots"]
        }

        # tat_nav_pose_master / trans_task_bat_threshold / charger pose map:
        # DB 가능하면 DB 우선, 없으면 JSON 폴백.
        db_poses = self._safe_repo_call_sync("load_tat_nav_poses") or {}
        db_thresholds = self._safe_repo_call_sync("load_trans_task_bat_thresholds") or {}
        db_charger_pose_map = self._safe_repo_call_sync("load_charger_pose_map") or {}
        self.transfer_points: dict[str, tuple[float, float]] = (
            dict(db_poses) if db_poses else dict(seed["transfer_points"])
        )
        self.battery_thresholds: dict = (
            dict(db_thresholds) if db_thresholds else dict(seed["battery_thresholds"])
        )
        self.charger_pose_map: dict[tuple[int, int], str] = (
            dict(db_charger_pose_map)
            if db_charger_pose_map
            else {
                (int(s["row"]), int(s["col"])): s["pose_nm"]
                for s in seed["charger_slots"]
                if s.get("pose_nm")
            }
        )

    async def _safe_repo_call(self, method_name: str, *args, **kwargs):
        """Repository의 비동기 메서드 호출."""
        if not self._db_ready or self._repo is None:
            return None
        method = getattr(self._repo, method_name, None)
        if method is None:
            return None
        try:
            result = method(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception:
            logger.exception("[StateManager] persistence %s failed", method_name)
            return None



    def _safe_repo_call_sync(self, method_name: str, *args, **kwargs):
        """Repository의 동기 메서드 호출."""
        if not self._db_ready or self._repo is None:
            return None
        sync_method = getattr(self._repo, f"{method_name}_sync", None)
        method = sync_method or getattr(self._repo, method_name, None)
        if method is None:
            return None
        try:
            return method(*args, **kwargs)
        except Exception:
            logger.exception("[StateManager] persistence %s failed", method_name)
            return None

    async def get_order_target_qty(self, ord_id: int) -> int | None:
        repo_qty = await self._safe_repo_call("get_order_target_qty", ord_id)
        if repo_qty is not None:
            normalized_qty = max(int(repo_qty), 1)
            self.orders.setdefault(ord_id, {})["target"] = normalized_qty
            return normalized_qty

        order_meta = self.orders.get(ord_id, {})
        target_qty = order_meta.get("target")
        if target_qty is None:
            return None
        return max(int(target_qty), 1)

    async def reserve_storage_slots(
        self,
        start_pos: tuple[int, int],
        target_qty: int,
    ) -> int | None:
        return await self._safe_repo_call(
            "reserve_storage_slots",
            int(start_pos[0]),
            int(start_pos[1]),
            int(target_qty),
        )

    async def get_empty_charger(self, res_id: str | None = None) -> tuple[int, int] | None:
        """충전 가능한 슬롯을 반환하고, 최초 조회 시 즉시 예약한다."""
        async with self._charger_lock:
            if res_id is not None:
                for chg_loc, slot_info in self.charger_slots.items():
                    if slot_info.get("res_id") == res_id:
                        return chg_loc

            for chg_loc, slot_info in self.charger_slots.items():
                if slot_info.get("status") == "empty":
                    reserved = await self._safe_repo_call(
                        "reserve_charger_slot",
                        int(chg_loc[0]),
                        int(chg_loc[1]),
                    )
                    if reserved is False:
                        continue
                    slot_info["status"] = "reserved"
                    slot_info["res_id"] = res_id
                    logger.info(
                        "[StateManager] Charger reserved: chg_loc=%s res_id=%s",
                        chg_loc,
                        res_id,
                    )
                    return chg_loc

            logger.warning("[StateManager] No empty charger available for res_id=%s", res_id)
            return None

    async def _release_charger(self, res_id: str | None = None) -> None:
        """충전 완료된 AMR의 슬롯 예약을 해제한다."""
        if res_id is None:
            return

        async with self._charger_lock:
            for chg_loc, slot_info in self.charger_slots.items():
                if slot_info.get("res_id") != res_id:
                    continue
                await self._safe_repo_call(
                    "release_charger_slot",
                    int(chg_loc[0]),
                    int(chg_loc[1]),
                    res_id,
                )
                slot_info["status"] = "empty"
                slot_info["res_id"] = None
                logger.info(
                    "[StateManager] Charger released: chg_loc=%s res_id=%s",
                    chg_loc,
                    res_id,
                )
                return

    async def _occupy_charger(self, res_id: str | None = None) -> None:
        """예약된 충전 슬롯을 AMR 점유 상태로 확정한다."""
        if res_id is None:
            return

        async with self._charger_lock:
            for chg_loc, slot_info in self.charger_slots.items():
                if slot_info.get("res_id") != res_id:
                    continue
                slot_info["status"] = "occupied"
                await self._safe_repo_call(
                    "occupy_charger_slot",
                    res_id,
                    int(chg_loc[0]),
                    int(chg_loc[1]),
                )
                logger.info(
                    "[StateManager] Charger occupied: chg_loc=%s res_id=%s",
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
            logger.info("[StateManager] Item %s status rolled back to CAST due to battery low", item_id)


    async def start_production(self, ord_id: int) -> StartProductionOrderAckModel:
        """Accept a positive order id and return a deterministic mock ack."""
        logger.info("[StateManager] start_production called for ord_id=%s", ord_id)
        if ord_id <= 0:
            return StartProductionOrderAckModel(
                ord_id=ord_id,
                accepted=False,
                reason="Invalid Order ID (Mock)",
            )

        if self._db_ready:
            return await self._start_production_db(ord_id)

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
                "strg_loc": None,
            }
            item_ids.append(item_id)

            equip_task_txn_ids.append(equip_task_txn_id)

        return StartProductionOrderAckModel(
            ord_id=ord_id,
            accepted=True,
            reason=f"Accepted by StateManager. created_items={len(item_ids)}",
            item_ids=item_ids,
            equip_task_txn_ids=[],
        )

    async def _start_production_db(self, ord_id: int) -> StartProductionOrderAckModel:
        if self._repo is None:
            return StartProductionOrderAckModel(
                ord_id=ord_id,
                accepted=False,
                reason="DB-backed start_production is not fully initialized.",
            )

        ack = await self._repo.start_production(ord_id)
        if not ack.accepted:
            return ack

        ptn_id = await self._safe_repo_call("get_order_ptn_loc_id", ord_id)
        for item_id in ack.item_ids:
            self._items[item_id] = {
                "item_id": item_id,
                "ord_id": ord_id,
                "order_id": ord_id,
                "flow_stat": "CREATED",
                "zone_nm": None,
                "result": None,
                "ptn_id": ptn_id,
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
        item = dict(self._items.get(item_id, {"item_id": item_id, "flow_stat": "CREATED"}))
        last_task_type = item.get("last_task_type") or item.get("task_type")
        if isinstance(last_task_type, str):
            try:
                last_task_type = TaskType(last_task_type)
            except ValueError:
                last_task_type = None
        else:
            last_task_type = None

        raw_flow = item.get("flow_stat") or item.get("cur_stat")
        if isinstance(raw_flow, FlowStatus):
            flow_stat = raw_flow
        else:
            try:
                flow_stat = FlowStatus(raw_flow) if raw_flow else FlowStatus.CREATED
            except ValueError:
                flow_stat = FlowStatus.CREATED

        return ItemStatusRecord(
            item_id=int(item["item_id"]),
            order_id=int(item.get("order_id") or item.get("ord_id") or 0),
            last_task_type=last_task_type,
            req_res_id=item.get("req_res_id"),
            flow_stat=flow_stat,
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
        task_id = _make_task_key(txn_id, task_input.task_type)
        if task_input.item_id is not None:
            item_meta = self._items.setdefault(task_input.item_id, {"item_id": task_input.item_id})
            item_meta.update({
                "txn_id": txn_id,
            })
        else:
            item_meta = {"item_id": task_input.item_id}
        task_meta = {
            "task_id": task_id,
            "txn_id": txn_id,
            "item_id": task_input.item_id,
            "ord_id": item_meta.get("ord_id") or item_meta.get("order_id"),
            "task_type": task_input.task_type,
            "status": task_input.txn_stat.value,
            "res_id": task_input.res_id,
            "chg_loc": task_input.chg_loc,
        }
        db_txn_id = await self._safe_repo_call("sync_task_created", dict(task_meta))
        if db_txn_id is not None:
            txn_id = int(db_txn_id)
            self._next_equip_task_txn_id = max(self._next_equip_task_txn_id, txn_id + 1)
            task_id = _make_task_key(txn_id, task_input.task_type)
            task_meta["task_id"] = task_id
            task_meta["txn_id"] = txn_id
            if task_input.item_id is not None:
                item_meta["txn_id"] = txn_id
        self._tasks[task_id] = task_meta
        return txn_id

    async def create_empty_item(self, order_id: int) -> int:
        db_item_id = await self._safe_repo_call(
            "create_empty_item",
            int(order_id),
        )

        if db_item_id is not None:
            item_id = int(db_item_id)
            self._next_item_id = max(self._next_item_id, item_id + 1)
        else:
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

    def get_available_resources(self, req_res_type: str) -> list[str]:
        # CONV쪽은 사람이 관리하기 때문에 항상 available
        if req_res_type == "CONV":
            return sorted(
                res_id
                for res_id, res_meta in self._res_list.items()
                if res_meta.get("res_type") == "CONV"
            )
        available = [
            res_id
            for res_id, res_meta in sorted(self._res_list.items())
            if res_meta.get("res_type") == req_res_type
            and res_meta.get("status") in {"IDLE", "TO_IDLE"}
            and res_meta.get("item_id") is None
        ]
        return available

    def get_amr_stats(self) -> list[AmrRuntimeState]:
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

    def get_all_robot_states(self) -> list[dict[str, Any]]:
        """PyQt 및 외부 클라이언트 조회를 위해 자원 상태(TAT 등)의 실시간 상태 리스트를 반환한다."""
        results = []
        for res_id, res_meta in sorted(self._res_list.items()):
            res_type = res_meta.get("res_type", "").upper()
            if res_type == "TAT":
                r_type = "amr"
            elif res_type in ("PAT", "MAT"):
                r_type = "manipulator"
            else:
                continue

            x = float(res_meta.get("x", 0.0))
            y = float(res_meta.get("y", 0.0))
            battery_pct = float(res_meta.get("battery_pct", 100))
            location = f"x={x:.2f}, y={y:.2f}" if (x != 0.0 or y != 0.0) else "-"

            status_str = res_meta.get("status", "IDLE")
            online_status = "offline" if status_str == "OFFLINE" else "online"

            results.append(
                {
                    "id": res_id,
                    "type": r_type,
                    "host": "ros2",
                    "status": online_status,
                    "battery": battery_pct,
                    "voltage": 0.0,
                    "location": location,
                    "task_state": res_meta.get("task_state", 1),
                    "task_id": str(res_meta.get("task_id") or ""),
                    "loaded_item": str(res_meta.get("item_id") or ""),
                }
            )
        return results

    def is_res_available(self, res_id: str) -> bool:
        res_meta = self._res_list.get(res_id)
        if res_meta is None:
            return False
        # CONV쪽은 사람이 관리하기 때문에 항상 available
        if res_meta.get("res_type") == "CONV":
            return True
        return res_meta.get("status") in {"IDLE", "TO_IDLE"} and res_meta.get("item_id") is None

    def get_res_type(self, res_id: str) -> str | None:
        """master(res 테이블) 기반의 res_type 조회. 어댑터 라우팅 등에서 사용."""
        meta = self._res_list.get((res_id or "").upper()) or self._res_list.get(res_id)
        if meta is None:
            return None
        return meta.get("res_type")

    def get_task_id_for_resource(self, res_id: str) -> str | None:
        """해당 자원에 현재 할당되어 진행 중인 task_id 를 반환. 없으면 None."""
        res_meta = self._res_list.get(res_id)
        if res_meta is None:
            return None
        raw = res_meta.get("task_id")
        return str(raw) if raw is not None else None

    async def update_amr_charger_return_state(
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
                "task_type": None,
            }
        )
        if source is not None:
            res_meta["charger_return_source"] = source
        if status in {"IDLE", "CHG"}:
            await self._occupy_charger(res_id)
        elif status == "FAIL":
            await self._release_charger(res_id)
        await self._safe_repo_call("sync_resource_snapshot", dict(res_meta))
        logger.info(
            "[StateManager] update_amr_charger_return_state: res=%s status=%s source=%s",
            res_id,
            status,
            source,
        )

    async def update_task_allocation(self, assign_input: AllocateTaskResInput) -> None:
        task_key = _make_task_key(assign_input.task_id, assign_input.task_type)
        task_meta = self._tasks.get(task_key)
        if task_meta is not None:
            task_meta["item_id"] = assign_input.item_id
            task_meta["res_id"] = assign_input.res_id
            task_meta["status"] = TxnStat.QUE.value
            task_meta["task_id"] = task_key
            task_meta["task_type"] = assign_input.task_type
            self._tasks[task_key] = task_meta

        self._res_list.setdefault(assign_input.res_id, {})
        self._res_list[assign_input.res_id].update(
            {
                "res_id": assign_input.res_id,
                "res_type": self._res_list.get(assign_input.res_id, {}).get("res_type"),
                "task_id": task_key,
                "item_id": assign_input.item_id,
                "status": "ALLOC",
            }
        )
        if task_meta is not None:
            await self._safe_repo_call("sync_task_status", dict(task_meta))
        await self._safe_repo_call(
            "sync_resource_snapshot",
            dict(self._res_list[assign_input.res_id]),
        )
        logger.info(
            "[StateManager] update_task_allocation: task=%s item=%s res=%s",
            assign_input.task_id,
            assign_input.item_id,
            assign_input.res_id,
        )

    async def update_task_status(self, req: UpdateTaskStatusInput) -> bool:
        task_key = _make_task_key(req.task_id, req.task_type)
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
                await self._handle_proc_task_status(req, task_meta, assigned_res_id)
            if req.new_stat == TxnStat.SUCC:
                suppress_task_completed = await self._handle_success_task_status(
                    task_meta,
                    previous_status,
                )
            if req.new_stat in {TxnStat.SUCC, TxnStat.FAIL}:
                logger.info(
                    "[StateManager] update_task_status DEBUG: task=%s status=%s task_type=%s assigned_res_id=%s res_meta=%s",
                    req.task_id,
                    req.new_stat.value,
                    task_meta.get("task_type"),
                    assigned_res_id,
                    self._res_list.get(assigned_res_id) if assigned_res_id else None,
                )
            if req.new_stat in {TxnStat.SUCC, TxnStat.FAIL} and assigned_res_id is not None:
                should_continue, suppress_resource_available = await self._handle_terminal_resource_status(
                    req,
                    task_meta,
                    assigned_res_id,
                )
                if not should_continue:
                    return True
        logger.info(
            "[StateManager] update_task_status: task=%s status=%s",
            req.task_id,
            req.new_stat.value,
        )
        if task_meta is not None:
            await self._safe_repo_call("sync_task_status", dict(task_meta))
        if (
            req.new_stat in {TxnStat.SUCC, TxnStat.FAIL}
            and assigned_res_id is not None
            and not suppress_resource_available
        ):
            self._publish_resource_available(req, task_meta, assigned_res_id)
        if (
            req.new_stat in {TxnStat.SUCC, TxnStat.FAIL}
            and self._event_bridge is not None
            and task_meta is not None
            and not suppress_task_completed
        ):
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

    async def record_adapter_result(self, req: AdapterStepResultInput) -> bool:
        """Adapter 결과를 메모리에 기록하고, 가능하면 DB 로그로 mirror한다."""
        task_key = _make_task_key(req.task_id, req.task_type)
        task_meta = self._tasks.get(task_key, {})
        record = req.model_dump()
        record["txn_id"] = task_meta.get("txn_id")
        record["ord_id"] = task_meta.get("ord_id")
        self.adapter_results.append(record)
        await self._safe_repo_call("record_adapter_result", record)
        logger.info(
            "[StateManager] record_adapter_result: task=%s step=%s action=%s success=%s",
            req.task_id,
            req.step_id,
            req.action,
            req.success,
        )
        return True

    async def _handle_proc_task_status(
        self,
        req: UpdateTaskStatusInput,
        task_meta: dict[str, Any],
        assigned_res_id: str,
    ) -> None:
        """Task 시작 시 따라오는 상태 반영과 이벤트 처리"""
        task_key = _make_task_key(req.task_id, req.task_type)
        current_task_type = task_meta.get("task_type")
        if current_task_type != TaskType.ToCHG:
            await self._release_charger(assigned_res_id)

        item_id = task_meta.get("item_id")
        if item_id is not None and current_task_type == TaskType.MM:
            item = self._items.setdefault(item_id, {"item_id": item_id})
            if item.get("flow_stat") == "CREATED":
                item["flow_stat"] = "CAST"
            item["last_task_type"] = task_meta.get("task_type")
            item["req_res_id"] = task_meta.get("res_id")
            await self._safe_repo_call("sync_item_snapshot", dict(item))

        res_meta = self._res_list.setdefault(assigned_res_id, {"res_id": assigned_res_id})
        if current_task_type == TaskType.ToCHG:
            # ToCHG는 무조건 배터리 부족 AMR이 충전소로 가는 작업
            res_meta.update(
                {
                    "task_id": task_key,
                    "item_id": None,
                    "status": "CHG",
                    "task_type": task_meta.get("task_type"),
                }
            )
        else:
            res_meta.update(
                {
                    "task_id": task_key,
                    "item_id": task_meta.get("item_id"),
                    "status": "ALLOC",
                    "task_type": task_meta.get("task_type"),
                }
            )
        await self._safe_repo_call("sync_resource_snapshot", dict(res_meta))

    async def _handle_success_task_status(
        self,
        task_meta: dict[str, Any],
        previous_status: str | None,
    ) -> bool:
        """Task 성공 시 따라오는 상태 반영과 이벤트 처리"""
        suppress_task_completed = task_meta.get("task_type") == TaskType.ToSTRG
        item_id = task_meta.get("item_id")
        if item_id is not None:
            item = self._items.setdefault(item_id, {"item_id": item_id})
            item["last_task_type"] = task_meta.get("task_type")
            item["req_res_id"] = (
                task_meta.get("res_id")
                if task_meta.get("task_type") in ITEM_AFFINITY_PREDECESSORS
                else None
            )
            await self._safe_repo_call(
                "sync_item_snapshot",
                {
                    "item_id": item_id,
                    "last_task_type": item.get("last_task_type"),
                    "req_res_id": item.get("req_res_id"),
                },
            )
        if previous_status != TxnStat.SUCC.value and task_meta.get("task_type") == TaskType.PA_GP:
            suppress_task_completed = await self._handle_pa_gp_completion(task_meta)
        return suppress_task_completed

    async def _handle_terminal_resource_status(
        self,
        req: UpdateTaskStatusInput,
        task_meta: dict[str, Any],
        assigned_res_id: str,
    ) -> tuple[bool, bool]:
        """Task 완료/실패 시 resource 상태를 갱신하고 RESOURCE_AVAILABLE 발행 여부를 결정한다."""
        suppress_resource_available = False
        task_key = _make_task_key(req.task_id, req.task_type)
        res_meta = self._res_list.setdefault(assigned_res_id, {"res_id": assigned_res_id})
        current_task_id = res_meta.get("task_id")
        if current_task_id not in {None, task_key}:
            await self._safe_repo_call("sync_task_status", dict(task_meta))
            logger.info(
                "[StateManager] resource reset skipped: task=%s res=%s current_task=%s",
                req.task_id,
                assigned_res_id,
                current_task_id,
            )
            return False, suppress_resource_available

        current_task_type = task_meta.get("task_type")
        if current_task_type == TaskType.ToCHG:
            # ToCHG는 무조건 배터리 부족 AMR이 충전소로 가는 작업
            await self._occupy_charger(assigned_res_id)
            res_meta.update(
                {
                    "task_id": None,
                    "item_id": None,
                    "status": "CHG",
                    "task_type": task_meta.get("task_type"),
                }
            )
            await self._safe_repo_call("sync_resource_snapshot", dict(res_meta))
            return True, True

        keep_item_affinity = (
            req.new_stat == TxnStat.SUCC
            and task_meta.get("task_type") in ITEM_AFFINITY_PREDECESSORS
        )
        res_meta.update(
            {
                "task_id": None,
                "status": "IDLE",
                "task_type": task_meta.get("task_type"),
            }
        )
        if keep_item_affinity:
            res_meta["item_id"] = task_meta.get("item_id")
            suppress_resource_available = True
        else:
            res_meta["item_id"] = None
        await self._safe_repo_call("sync_resource_snapshot", dict(res_meta))

        return True, suppress_resource_available

    def _publish_resource_available(
        self,
        req: UpdateTaskStatusInput,
        task_meta: dict[str, Any] | None,
        assigned_res_id: str,
    ) -> None:
        """Terminal task/item/resource DB 반영 후 resource 가용 이벤트를 발행한다."""
        if self._event_bridge is None or task_meta is None:
            return

        req_res_type = self._res_list.get(assigned_res_id, {}).get("res_type")
        logger.info(
            "[StateManager] terminal_resource DEBUG: task=%s res=%s req_res_type=%s event_bridge_set=%s",
            req.task_id,
            assigned_res_id,
            req_res_type,
            self._event_bridge is not None,
        )
        if not isinstance(req_res_type, str) or not req_res_type:
            return

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

    async def _handle_pa_gp_completion(self, task_meta: dict[str, Any]) -> bool:
        """PA_GP 성공 시 DB 적재 슬롯을 occupied로 확정하고, gp_qty를 증가시켜 주문 생산 완료 여부를 판단."""
        ord_id = task_meta.get("ord_id")
        if ord_id is None:
            return False

        item_id = task_meta.get("item_id")
        if item_id is not None:
            item = self._items.setdefault(item_id, {"item_id": item_id})
            strg_loc = _storage_loc_tuple(item.get("strg_loc"))
            if isinstance(strg_loc, tuple):
                await self._safe_repo_call(
                    "update_item_storage_location",
                    item_id,
                    int(strg_loc[0]),
                    int(strg_loc[1]),
                )
            # PA_GP 성공 == 적재 완료. item.cur_stat='STORED' 로 라이프사이클 전이.
            item["flow_stat"] = "STORED"
            item["last_task_type"] = task_meta.get("task_type")
            await self._safe_repo_call("sync_item_snapshot", dict(item))

        return await self._increment_order_gp_qty(int(ord_id))

    async def _increment_order_gp_qty(self, ord_id: int) -> bool:
        """DB 원자 증가 결과를 반영하고 최초 완료 후처리를 수행한다."""
        db_complete = await self._safe_repo_call("increment_order_gp_qty", ord_id)
        if isinstance(db_complete, dict):
            completed = bool(db_complete.get("completed"))
            gp_qty = db_complete.get("gp_qty")
            target_qty = db_complete.get("qty")
            logger.info(
                "[StateManager] PA_GP completion: ord_id=%s gp_qty=%s qty=%s "
                "completed=%s",
                ord_id,
                gp_qty,
                target_qty,
                completed,
            )
            if completed:
                logger.info(
                    "[StateManager] ord_id=%s qty=%s 생산완료",
                    ord_id,
                    target_qty,
                )

            return completed

        order_state = self.orders.setdefault(ord_id, {})
        gp_qty = int(order_state.get("gp_qty", 0)) + 1
        order_state["gp_qty"] = gp_qty
        target_qty = order_state.get("target")
        completed = target_qty is not None and gp_qty == int(target_qty)
        logger.info(
            "[StateManager] PA_GP completion(memory): ord_id=%s gp_qty=%s qty=%s completed=%s",
            ord_id,
            gp_qty,
            target_qty,
            completed,
        )
        if completed:
            logger.info(
                "[StateManager] ord_id=%s qty=%s 생산완료",
                ord_id,
                target_qty,
            )

        return completed

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
            "[StateManager] update_inspection_image: item_id=%s keys=%s",
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
                "[StateManager] record_inspection_result: invalid item_id=%s — skip",
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
                "[StateManager] record_inspection_result: import 실패 item_id=%d exc=%s",
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
                    "[StateManager] record_inspection_failure 실패 item_id=%d exc=%s",
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
                segmented_image_b64=inference.get("segmented_image_b64"),
                result_image_b64=inference.get("result_image_b64"),
            )
            logger.info(
                "[StateManager] record_inspection_result OK: item_id=%d insp_txn=%d "
                "result=%s class=%s",
                item_id_int,
                recorded.insp_txn_id,
                recorded.result,
                recorded.predicted_class or "<none>",
            )
            return True
        except (LookupError, ValueError) as exc:
            logger.warning(
                "[StateManager] record_inspection_result 실패 item_id=%d exc=%s — FAIL 기록 시도",
                item_id_int,
                exc,
            )
            try:
                cmd.record_inspection_failure(item_id=item_id_int, reason=str(exc))
            except Exception as exc2:  # noqa: BLE001
                logger.warning(
                    "[StateManager] record_inspection_failure (after persist err) 실패 "
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
        task_type: TaskType,
    ) -> bool:
        if self._event_bridge is None:
            return False

        task_key = _make_task_key(task_id, task_type)
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
                    "subtask_type": subtask_type,
                    "task_type": task_type,
                },
            )
        )
        logger.info(
            "[StateManager] publish_subtask_completed: task=%s subtask_type=%s item_id=%s",
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
        res_meta["status"] = "IDLE"
        if task_id is not None:
            res_meta["task_id"] = task_id
        if item_id is not None:
            res_meta["item_id"] = item_id
        await self._safe_repo_call("sync_resource_snapshot", dict(res_meta))
        await self._release_charger(res_id)

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
            "[StateManager] publish_amr_charged: resource=%s task=%s source=%s",
            res_id,
            task_id,
            source,
        )
        return True

    async def update_item_status(
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
        await self._safe_repo_call("sync_item_snapshot", dict(item))
        logger.info(
            "[StateManager] update_item_status: item=%s flow_stat=%s zone_nm=%s",
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

        logger.debug(
            "[StateManager] update_amr_runtime_memory: res=%s x=%s y=%s battery=%s",
            res_id,
            x,
            y,
            battery_pct,
        )

    async def sync_resource_telemetry(self, telemetry: dict[str, Any]) -> None:
        """ROS에서 수신된 AMR의 배터리 정보를 repository에 반영한다."""
        await self._safe_repo_call("sync_resource_telemetry", telemetry)

    async def load_ship_item_locations(self, ord_id: int) -> list[tuple[int, int, int]]:
        """주문의 출고 대상 item 적재 위치를 DB 에서 직접 조회.

        cur_stat='STORED' 인데 strg_location_stat 매핑이 풀린 경우를 best-effort
        self-heal 한 뒤, occupied 슬롯이 있는 item 들의 (item_id, row, col) 을 모은다.
        """
        rebound = await self._safe_repo_call("auto_rebind_storage_for_order", ord_id)
        if rebound:
            logger.info("[StateManager] auto_rebind: ord_id=%s rebound=%s", ord_id, rebound)

        shipping_marked = await self._safe_repo_call("mark_order_shipping", ord_id)
        logger.info(
            "[StateManager] mark_order_shipping called: ord_id=%s result=%s",
            ord_id,
            shipping_marked,
        )
        rows = await self._safe_repo_call("load_order_items_with_strg", ord_id) or []
        result: list[tuple[int, int, int]] = []
        for r in rows:
            strg_loc = _storage_loc_tuple(r.get("strg_loc"))
            if not isinstance(strg_loc, tuple) or len(strg_loc) != 2:
                continue
            
            item_id = int(r["item_id"])
            self._items[item_id] = {
                **self._items.get(item_id, {}),
                "item_id": item_id,
                "ord_id": int(r.get("ord_id") or ord_id),
                "order_id": int(r.get("order_id") or r.get("ord_id") or ord_id),
                "flow_stat": r.get("flow_stat") or r.get("cur_stat"),
                "cur_stat": r.get("cur_stat") or r.get("flow_stat"),
                "strg_loc": strg_loc,
            }
            result.append((int(r["item_id"]), int(strg_loc[0]), int(strg_loc[1])))
        logger.info(
            "[StateManager] load_ship_item_locations ord_id=%s rows=%d eligible=%d",
            ord_id,
            len(rows),
            len(result),
        )
        return result

    # 적재 예정 위치를 메모리에 기록한다. DB 점유 확정은 PA_GP 성공 시 처리한다.
    async def update_item_storage_location(self, item_id: int, strg_loc: tuple[int, int] | str) -> None:
        item = self._items.setdefault(item_id, {"item_id": item_id})
        item["strg_loc"] = _storage_loc_tuple(strg_loc)

        logger.info(
            "[StateManager] item storage location saved: item=%s strg_loc=%s",
            item_id,
            item["strg_loc"],
        )

    # 연속 빈 슬롯의 시작 위치 반환
    async def get_empty_start_slot(self, target_qty: int) -> tuple[int, int] | None:
        """
        DB 기준으로 빈 적재 슬롯을 조회하고,
        주문 수량만큼 row-major 순서로 연속된 공간이 있으면 가장 작은 위치를 반환한다.
        """
        if target_qty <= 0:
            return None

        db_slots = await self._safe_repo_call("get_storage_slots")

        # DB 조회 실패 시 메모리로
        if db_slots is None:
            logger.warning("[StateManager] DB slot 조회 실패 또는 repo 없음")
            if self.task_manager is None:
                return None

            empty_slots = [
                (row, col)
                for (row, col), slot in sorted(self.task_manager.slot_table.items())
                if slot.get("status") == "empty"
                and slot.get("order_id") is None
            ]
        # DB 조회
        else:
            empty_slots = [
                (int(slot["row"]), int(slot["col"]))
                for slot in db_slots
                if slot.get("status") == "empty"
                and slot.get("order_id") is None
            ]

        empty_slots.sort()

        if len(empty_slots) < target_qty:
            return None

        # row-major 연속 빈 구간 탐색
        max_col = int(getattr(self.task_manager, "MAX_COL", 0) or 0)
        if max_col <= 0 and db_slots is not None:
            max_col = max((int(slot["col"]) for slot in db_slots), default=0)
        if max_col <= 0:
            max_col = max((col for _, col in empty_slots), default=0)
        if max_col <= 0:
            return None

        empty_slot_set = set(empty_slots)
        for start_row, start_col in empty_slots:
            start_idx = (start_row - 1) * max_col + (start_col - 1)
            required_positions = {
                ((start_idx + offset) // max_col + 1, (start_idx + offset) % max_col + 1)
                for offset in range(target_qty)
            }
            if required_positions.issubset(empty_slot_set):
                return (start_row, start_col)

        return None
    #item 상태 출고 완료 /보관 위치 제거 /rack slot 해제
    async def complete_ship_batch(self, batch: list[tuple[int, int, int]]) -> None:
        for item_id, row, col in batch:
            item = self._items.setdefault(item_id, {"item_id": item_id})
            completed_item = dict(item)
            completed_item["flow_stat"] = "READY_TO_SHIP"
            completed_item["cur_stat"] = "READY_TO_SHIP"
            completed_item["strg_loc"] = None
            completed_item["last_task_type"] = TaskType.PICK

            # DB 출고 처리 후 메모리 반영
            await self._safe_repo_call("sync_item_snapshot", completed_item)
            released = await self._safe_repo_call("release_storage_slot_for_item", item_id)

            item.update(completed_item)

            logger.info(
                "[SHIP DEBUG] complete item: item_id=%s loc=(%s,%s)",
                item_id,
                row,
                col,
            )

            logger.info(
                "[StateManager] complete_ship_batch: item=%s loc=(%s,%s) released=%s",
                item_id,
                row,
                col,
                released,
            )

    async def complete_shipping_order_if_ready(self, ord_id: int) -> bool:
        completed = await self._safe_repo_call(
            "mark_order_completed_if_shipping_finished",
            ord_id,
        )
        logger.info(
            "[StateManager] complete_shipping_order_if_ready: ord_id=%s result=%s",
            ord_id,
            completed,
        )
        return bool(completed)
