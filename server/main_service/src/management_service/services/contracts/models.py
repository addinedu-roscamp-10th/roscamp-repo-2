from __future__ import annotations

from datetime import date, datetime,UTC
from decimal import Decimal
from typing import Any, Optional,Dict
from dataclasses import dataclass, field
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field , field_validator

from .enums import (
    AdminActionType,
    AlertSeverity,
    EquipStat,
    EquipTaskType,
    LocStatus,
    OrdStat,
    OrdTxnType,
    PoseNm,
    RfidParseStatus,
    TransStat,
    TransTaskType,
    TxnStat,
    TaskType,
    EventType,

)
"""
# ─── Order ────────────────────────────────────────────────────────────────────
class CreateOrdInput(BaseModel):
    user_id: int


class OrdRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ord_id: int
    user_id: int
    created_at: datetime


class CreateOrdDetailInput(BaseModel):
    ord_id: int
    prod_id: Optional[int] = None
    diameter: Optional[Decimal] = None
    thickness: Optional[Decimal] = None
    material: Optional[str] = None
    load_class: Optional[str] = None
    qty: Optional[int] = Field(default=None, gt=0)
    final_price: Optional[Decimal] = None
    due_date: Optional[date] = None
    ship_addr: Optional[str] = None


class OrdDetailRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ord_id: int
    prod_id: Optional[int] = None
    diameter: Optional[Decimal] = None
    thickness: Optional[Decimal] = None
    material: Optional[str] = None
    load_class: Optional[str] = None
    qty: Optional[int] = None
    final_price: Optional[Decimal] = None
    due_date: Optional[date] = None
    ship_addr: Optional[str] = None


class CreateOrdPpMapInput(BaseModel):
    ord_id: int
    pp_id: int


class OrdPpMapRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    map_id: int
    ord_id: int
    pp_id: int


class CreateOrdTxnInput(BaseModel):
    ord_id: int
    txn_type: OrdTxnType = OrdTxnType.RCVD


class OrdTxnRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    txn_id: int
    ord_id: int
    txn_type: OrdTxnType
    txn_at: datetime


class CreateOrdStatInput(BaseModel):
    ord_id: int
    user_id: Optional[int] = None
    ord_stat: OrdStat = OrdStat.RCVD


class UpdateOrdStatInput(BaseModel):
    ord_stat: OrdStat
    user_id: Optional[int] = None


class OrdStatRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stat_id: int
    ord_id: int
    user_id: Optional[int] = None
    ord_stat: Optional[OrdStat] = None
    updated_at: datetime


class CreateOrdLogInput(BaseModel):
    ord_id: int
    prev_stat: Optional[OrdStat] = None
    new_stat: Optional[OrdStat] = None
    changed_by: Optional[int] = None


class OrdLogRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    log_id: int
    ord_id: int
    prev_stat: Optional[OrdStat] = None
    new_stat: Optional[OrdStat] = None
    changed_by: Optional[int] = None
    logged_at: datetime


# ─── Item ─────────────────────────────────────────────────────────────────────


class CreateItemStatInput(BaseModel):
    ord_id: int
    qty: int = Field(gt=0)


class UpdateItemStatInput(BaseModel):
    flow_stat: Optional[str] = None
    zone_nm: Optional[str] = None
    result: Optional[bool] = None


class ItemStatRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_stat_id: int
    ord_id: int
    flow_stat: Optional[str] = None
    zone_nm: Optional[str] = None
    result: Optional[bool] = None
    updated_at: Optional[datetime] = None


# ─── Equip ────────────────────────────────────────────────────────────────────


class AssignEquipTaskInput(BaseModel):
    res_id: str
    task_type: EquipTaskType
    item_stat_id: Optional[int] = None
    strg_loc_id: Optional[int] = None
    ship_loc_id: Optional[int] = None


class UpdateEquipTaskInput(BaseModel):
    txn_stat: TxnStat
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class EquipTaskTxnRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    txn_id: int
    res_id: Optional[str] = None
    task_type: EquipTaskType
    txn_stat: Optional[TxnStat] = None
    item_stat_id: Optional[int] = None
    strg_loc_id: Optional[int] = None
    ship_loc_id: Optional[int] = None
    req_at: datetime
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class CreateEquipStatInput(BaseModel):
    res_id: str
    cur_stat: EquipStat = EquipStat.IDLE


class UpdateEquipStatInput(BaseModel):
    cur_stat: Optional[EquipStat] = None
    item_stat_id: Optional[int] = None
    txn_type: Optional[str] = None
    err_msg: Optional[str] = None


class EquipStatRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stat_id: int
    res_id: str
    item_stat_id: Optional[int] = None
    txn_type: Optional[str] = None
    cur_stat: Optional[EquipStat] = None
    updated_at: datetime
    err_msg: Optional[str] = None


# ─── Trans ────────────────────────────────────────────────────────────────────


class AssignTransTaskInput(BaseModel):
    res_id: str
    task_type: TransTaskType
    item_stat_id: Optional[int] = None
    ord_id: Optional[int] = None
    chg_loc_id: Optional[int] = None


class UpdateTransTaskInput(BaseModel):
    txn_stat: TxnStat
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class TransTaskTxnRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    txn_id: int
    res_id: Optional[str] = None
    task_type: TransTaskType
    txn_stat: Optional[TxnStat] = None
    chg_loc_id: Optional[int] = None
    item_stat_id: Optional[int] = None
    ord_id: Optional[int] = None
    req_at: datetime
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class CreateTransStatInput(BaseModel):
    res_id: str
    cur_stat: TransStat = TransStat.IDLE
    battery_pct: Optional[int] = None
    item_stat_id: Optional[int] = None
    cur_trans_coord_id: Optional[int] = None


class UpdateTransStatInput(BaseModel):
    cur_stat: Optional[TransStat] = None
    battery_pct: Optional[int] = None
    item_stat_id: Optional[int] = None
    cur_trans_coord_id: Optional[int] = None


class TransStatRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    res_id: str
    item_stat_id: Optional[int] = None
    cur_stat: Optional[TransStat] = None
    battery_pct: Optional[int] = None
    cur_trans_coord_id: Optional[int] = None
    updated_at: datetime


# ─── Inspection ───────────────────────────────────────────────────────────────


class CreateInspTaskInput(BaseModel):
    item_stat_id: Optional[int] = None
    res_id: Optional[str] = None


class UpdateInspTaskInput(BaseModel):
    txn_stat: TxnStat
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class InspTaskTxnRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    txn_id: int
    item_stat_id: Optional[int] = None
    res_id: Optional[str] = None
    txn_stat: Optional[TxnStat] = None
    req_at: datetime
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


# ─── PP Task ──────────────────────────────────────────────────────────────────


class CreatePpTaskInput(BaseModel):
    ord_id: int
    map_id: Optional[int] = None
    pp_nm: Optional[str] = None
    item_stat_id: Optional[int] = None
    operator_id: Optional[int] = None


class UpdatePpTaskInput(BaseModel):
    txn_stat: TxnStat
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class PpTaskTxnRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    txn_id: int
    ord_id: int
    map_id: Optional[int] = None
    pp_nm: Optional[str] = None
    item_stat_id: Optional[int] = None
    operator_id: Optional[int] = None
    txn_stat: Optional[TxnStat] = None
    req_at: datetime
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


# ─── Location Stat ────────────────────────────────────────────────────────────


class UpdateChgLocStatInput(BaseModel):
    status: LocStatus


class ChgLocStatRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    loc_id: int
    zone_id: Optional[int] = None
    res_id: Optional[str] = None
    loc_row: Optional[int] = None
    loc_col: Optional[int] = None
    status: Optional[LocStatus] = None
    stored_at: datetime


class UpdateStrgLocStatInput(BaseModel):
    item_stat_id: Optional[int] = None
    status: LocStatus


class StrgLocStatRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    loc_id: int
    zone_id: Optional[int] = None
    item_stat_id: Optional[int] = None
    loc_row: Optional[int] = None
    loc_col: Optional[int] = None
    status: Optional[LocStatus] = None
    stored_at: datetime


class UpdateShipLocStatInput(BaseModel):
    item_stat_id: Optional[int] = None
    ord_id: Optional[int] = None
    status: LocStatus


class ShipLocStatRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    loc_id: int
    zone_id: Optional[int] = None
    ord_id: Optional[int] = None
    item_stat_id: Optional[int] = None
    loc_row: Optional[int] = None
    loc_col: Optional[int] = None
    status: Optional[LocStatus] = None
    stored_at: datetime


# ─── Alerts ───────────────────────────────────────────────────────────────────


class CreateAlertInput(BaseModel):
    id: str
    res_id: str
    type: str
    severity: AlertSeverity = AlertSeverity.INFO
    error_code: str = ""
    message: str
    abnormal_value: str = ""
    zone: Optional[str] = None
    timestamp: str


class UpdateAlertInput(BaseModel):
    acknowledged: Optional[bool] = None
    resolved_at: Optional[str] = None


class AlertsStatRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    res_id: str
    type: str
    severity: AlertSeverity
    error_code: str
    message: str
    abnormal_value: str
    zone: Optional[str] = None
    timestamp: str
    resolved_at: Optional[str] = None
    acknowledged: bool


# ─── Log Events ───────────────────────────────────────────────────────────────


class UserActionEvent(BaseModel):
    user_id: int
    screen_nm: str
    action_type: str
    ref_id: Optional[int] = None


class OperatorHandoffEvent(BaseModel):
    operator_id: int
    item_stat_id: Optional[int] = None
    pp_task_txn_id: Optional[int] = None
    button_device_id: Optional[str] = None
    idempotency_key: Optional[str] = None


class RfidScanEvent(BaseModel):
    reader_id: str
    zone: Optional[str] = None
    raw_payload: str
    ord_id: Optional[str] = None
    item_key: Optional[str] = None
    item_stat_id: Optional[int] = None
    parse_status: RfidParseStatus
    idempotency_key: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class AdminActionEvent(BaseModel):
    admin_id: int
    target_table: str
    target_id: Optional[str] = None
    action_type: AdminActionType
    old_value: Optional[dict[str, Any]] = None
    new_value: Optional[dict[str, Any]] = None


class SystemEvent(BaseModel):
    component: str
    event_type: str
    txn_id: Optional[int] = None
    detail: Optional[str] = None


class EquipDataRecord(BaseModel):
    res_id: str
    txn_id: Optional[int] = None
    sensor_type: str
    raw_value: Decimal
    physical_value: Optional[Decimal] = None
    unit: Optional[str] = None
    status: Optional[str] = None


class TransDataRecord(BaseModel):
    res_id: str
    txn_id: Optional[int] = None
    sensor_type: str
    raw_value: Decimal
    physical_value: Optional[Decimal] = None
    unit: Optional[str] = None
    status: Optional[str] = None


class EquipErrorRecord(BaseModel):
    res_id: Optional[str] = None
    task_txn_id: Optional[int] = None
    failed_stat: Optional[str] = None
    err_msg: Optional[str] = None


class TransErrorRecord(BaseModel):
    res_id: Optional[str] = None
    task_txn_id: Optional[int] = None
    failed_stat: Optional[str] = None
    err_msg: Optional[str] = None
    battery_pct: Optional[int] = None

"""

## Task manager

class ItemStatusRecord(BaseModel): #ok -> tm
    item_id: int
    order_id: int  # 어느 주문에 속한 아이tuple인지 추적하기 위해 추가
    last_task_type: Optional[TaskType] = None
    flow_stat: Optional[str] = None
    is_defective: bool = False
    ptn_id: Optional[int] = None

class CreateTaskInput(BaseModel): #tm -> sm
    item_id: int
    task_type: TaskType
    strg_loc: Optional[str] = None
    #ship_loc: Optional[str] = None
    txn_stat: str = "que"
    res_id : Optional[int] = None

class NextTaskResult(BaseModel): # tm -> ok
    item_id: int
    txn_id: int
    task_type: TaskType
    priority: int = 5
    strg_loc: Optional[str] = None


##Event bridge
class Event(BaseModel):
    """EventBridge 가 라우팅하는 일반 이벤트 메시지.

    구체 *Event (TaskCompletedEvent 등) 는 payload 안에 dict 로 들어가거나,
    EventType 만으로 충분한 경우 payload 가 비어있을 수 있다.
    """
    event_type: EventType
    txn_id: Optional[int] = None
    ord_id: Optional[int] = None
    item_id: Optional[int] = None
    resource_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PublishResult(BaseModel):
    """EventBridgeImpl.publish 의 반환 — 호출/성공/실패 카운트."""
    event_type: EventType
    handlers_invoked: int
    handlers_success: int
    handlers_failed: int
    occurred_at: datetime

##Task Executor
class CommandStep(BaseModel):
    """분해된 물리 시퀀스의 단위 단계 (인메모리 정의)"""
    step_id: int = Field(gt=0)
    action: str = Field(min_length=1)
    params: Dict[str, Any] = Field(default_factory=dict)
    timeout_sec: int = Field(default=30, ge=1)

class TaskExecutorInput(BaseModel):
    task_id: str  # min_length 제거
    res_id: str   # min_length 제거
    task_type: TaskType
    item_id: Optional[str] = None
    
    @field_validator("task_id", "res_id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not v or not v.strip():  # None/빈문자열/공백만 체크
            raise ValueError("ID cannot be empty")
        return v.strip()

class ExecutionResult(BaseModel):
    """실행 완료 후 반환되는 결과값 ([동사][명사]Result 규칙)"""
    task_id: str
    final_status: TxnStat
    steps_executed: int = Field(ge=0)
    error_code: Optional[str] = None
    completed_at: datetime = Field(default_factory=datetime.now)

class UpdateTaskStatusInput(BaseModel):
    """State Manager 로 보낼 Task 진행 상태 업데이트 요청 ([동사][명사]Input 규칙)"""
    task_id: str
    new_stat: TxnStat
    error_code: Optional[str] = None





""" dataclass도 여기 포함????"""
@dataclass
class HandlerMeta():
    """내부 구독자 관리용 메타. dataclass — Callable 객체를 Pydantic 에 넣기 부적합."""

    event_type: EventType
    handler: Callable[[Event], None]
    subscriber_name: str
    registered_at: datetime = field(default_factory=lambda: datetime.now(UTC))

