from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Dict
from dataclasses import dataclass, field
from collections.abc import Callable, Coroutine

from pydantic import BaseModel, Field , field_validator, model_validator


from .enums import (
    TxnStat,
    TaskType,
    EventType,
    FlowStatus,
)

## Orchestrator

class StartProductionOrderAckModel(BaseModel):
    ord_id: int
    accepted: bool
    reason: str | None = None
    item_ids: list[int] = Field(default_factory=list)
    equip_task_txn_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_singular_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        upgraded = dict(data)
        item_id = upgraded.pop("item_id", None)
        equip_task_txn_id = upgraded.pop("equip_task_txn_id", None)

        if "item_ids" not in upgraded and item_id is not None:
            upgraded["item_ids"] = [item_id]
        if "equip_task_txn_ids" not in upgraded and equip_task_txn_id is not None:
            upgraded["equip_task_txn_ids"] = [equip_task_txn_id]
        return upgraded

    @property
    def item_id(self) -> int | None:
        return self.item_ids[0] if self.item_ids else None

    @property
    def item_count(self) -> int:
        return len(self.item_ids)

    @property
    def equip_task_txn_id(self) -> int | None:
        return self.equip_task_txn_ids[0] if self.equip_task_txn_ids else None


class StartProductionBatchAckModel(BaseModel):
    requested_count: int
    accepted_count: int
    rejected_count: int
    orders: list[StartProductionOrderAckModel] = Field(default_factory=list)
    message: str | None = None

class ScheduleNextTaskInput(BaseModel):
    item_id: int
    planning_event: str | None = None
    last_task_type: TaskType | None = None
    req_res_id: str | None = None

class TaskInfo(BaseModel):
    task_id: str
    task_type: str
    req_res_type: str | None = None
    priority: int = 0
    rack_pos: str | None = None


## Task manager

class ItemStatusRecord(BaseModel): #ok -> tm
    item_id: int
    order_id: int  # 어느 주문에 속한 아이tuple인지 추적하기 위해 추가
    last_task_type: Optional[TaskType] = None
    req_res_id: str | None = None
    flow_stat: FlowStatus = FlowStatus.CREATED
    is_defective: bool = False
    ptn_id: Optional[int] = None
    strg_loc: Optional[tuple[int, int]] = None
    ship_loc: Optional[tuple[int, int]] = None

class CreateTaskInput(BaseModel): #tm -> sm TASK 정보 중 디비에 기록할것
    item_id: Optional[int] = None
    task_type: TaskType
    txn_stat: TxnStat = TxnStat.QUE
    res_id : Optional[int] = None
    chg_loc: Optional[tuple[int, int]] = None

class NextTaskResult(BaseModel): # tm -> ok
    item_id: int
    txn_id: int
    task_type: TaskType
    priority: int = 5
    strg_loc: Optional[str] = None
    chg_loc : Optional[tuple[int, int]] = None #ToCHG 일때 할당받는 주차 자리(row, col)
    batch: list[tuple[int, int, int]] | None = None


class ShipTaskResult(BaseModel): #tm -> ok 출고 Task 정보
    txn_id: int
    task_type: TaskType
    priority: int = 3
    batch: list[tuple[int, int, int]] | None = None


## Event bridge
class Event(BaseModel):
    """EventBridge 가 라우팅하는 일반 이벤트 메시지.

    구체 *Event (TaskCompletedEvent 등) 는 payload 안에 dict 로 들어가거나,
    EventType 만으로 충분한 경우 payload 가 비어있을 수 있다.
    """
    event_type: EventType
    txn_id: Optional[int] = None
    ord_id: Optional[int] = None
    item_id: Optional[int] = None
    res_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PublishResult(BaseModel):
    """EventBridgeImpl.publish 의 반환 — 호출/성공/실패 카운트."""
    event_type: EventType
    handlers_invoked: int
    handlers_success: int
    handlers_failed: int
    occurred_at: datetime

## Task Executor
class CommandStep(BaseModel):
    """분해된 물리 시퀀스의 단위 단계 (인메모리 정의)"""
    step_id: int = Field(gt=0)
    action: str = Field(min_length=1)
    params: Dict[str, Any] = Field(default_factory=dict)
    timeout_sec: int = Field(default=30, ge=1)

class ExecuteTaskInput(BaseModel):
    task_id: str  # min_length 제거
    res_id: str   # min_length 제거
    task_type: TaskType
    item_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    
    @field_validator("task_id", "res_id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not v or not v.strip():  # None/빈문자열/공백만 체크
            raise ValueError("ID cannot be empty")
        return v.strip()

class ExecutionResult(BaseModel):
    """실행 완료 후 반환되는 결과값 ([동사][명사]Result 규칙)"""
    task_id: str
    task_type: Optional[TaskType] = None
    final_status: TxnStat
    steps_executed: int = Field(ge=0)
    error_code: Optional[str] = None
    completed_at: datetime = Field(default_factory=datetime.now)

class AdapterResult(BaseModel):
    success: bool
    message: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)

class AdapterStepResultInput(BaseModel):
    """Adapter command result recorded after each executor step."""
    task_id: str
    item_id: Optional[int] = None
    res_id: str
    task_type: TaskType
    step_id: int
    action: str
    success: bool
    message: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    command_params: Dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class UpdateTaskStatusInput(BaseModel):
    """State Manager 로 보낼 Task 진행 상태 업데이트 요청 ([동사][명사]Input 규칙)"""
    task_id: str
    task_type: TaskType
    new_stat: TxnStat
    error_code: Optional[str] = None

## Task Allocator
class AllocateTaskInput(BaseModel):
    task_id: str
    item_id: int
    req_res_id: str | None = None
    zone_nm: str | None = None
    task_type: TaskType

class AllocateTaskResInput(BaseModel):
    task_id: str
    task_type: TaskType
    item_id: int
    res_id: str

class AllocateTaskResult(BaseModel):
    success: bool
    req_res_type: str
    res_id: str | None = None
    reason: str | None = None

class AmrRuntimeState(BaseModel):
    res_id: str
    x: float
    y: float
    bat_pct: int

""" dataclass도 여기 포함????"""
@dataclass
class HandlerMeta():
    """내부 구독자 관리용 메타. dataclass — Callable 객체를 Pydantic 에 넣기 부적합."""

    event_type: EventType
    handler: Callable[[Event], Coroutine[Any, Any, None] | None]
    subscriber_name: str
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
