from __future__ import annotations

from enum import Enum

##task manager
class TaskType(str, Enum):
    # 생산 및 장비 작업 [equip]
    MM = "MM"
    POUR = "POUR"
    DM = "DM"
    PP = "PP"
    INSP = "INSP"
    ToPAWait = "ToPAWait"
    PA_GP = "PA_GP"
    PA_DP = "PA_DP"
    PICK = "PICK"
    
    # 이송 및 이동 작업 [trans]
    ToPP = "ToPP"
    ToINSP = "ToINSP"
    ToSTRG = "ToSTRG"
    ToSHIP = "ToSHIP"
    ToCHG = "ToCHG"

    #출고
    SHIP = "SHIP"


class FlowStatus(str, Enum):
    CREATED = "CREATED"
    CAST = "CAST"
    WAIT_PP = "WAIT_PP"
    PP = "PP"
    WAIT_INSP = "WAIT_INSP"
    INSP = "INSP"
    WAIT_PA = "WAIT_PA"
    PA = "PA"
    STORED = "STORED"
    DISCARDED = "DISCARDED"
    PICK = "PICK"
    READY_TO_SHIP = "READY_TO_SHIP"

## Event Bridge
class EventType(str, Enum):
    TASK_COMPLETED = "TASK_COMPLETED"
    SUBTASK_COMPLETED = "SUBTASK_COMPLETED"
    ITEM_STATUS_CHANGED = "ITEM_STATUS_CHANGED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    RESOURCE_AVAILABLE = "RESOURCE_AVAILABLE"
    AMR_CHARGED = "AMR_CHARGED"
    AMR_BATTERY_LOW = "AMR_BATTERY_LOW"
    ARM_RETURN_COMPLETED = "ARM_RETURN_COMPLETED"
    HANDOFF_ACK = "HANDOFF_ACK"
    PP_DONE_REQUESTED = "PP_DONE_REQUESTED"
    ITEM_LOOKUP_REQUESTED = "ITEM_LOOKUP_REQUESTED"
    # ===== EventGateway external 채널 추가 (PR #7 / PR #8 통합 검증 후, 2026-05-11) =====
    # PyQt(Monitoring) ↔ Management ↔ Jetson(Vision) 양방향 EventBridge wire 매핑.
    RFID_SCANNED = "RFID_SCANNED"                     # RC522 스캔 (ESP→Jetson→Backend→PyQt)
    TOF1_ENTRY = "TOF1_ENTRY"                         # 카메라 앞 진입 (ESP→Jetson→Backend→PyQt)
    INSP_COMPLETED = "INSP_COMPLETED"                 # 검사 완료 → ESP32 RUN (Backend→Jetson)
    ITEM_LOOKUP_RESULT = "ITEM_LOOKUP_RESULT"         # ITEM_LOOKUP_REQUESTED 응답 (Backend→PyQt)
    # 카메라 밑 캡처 이미지가 backend 에 도착 + 디스크 저장까지 완료된 시점.
    # publisher: UploadInspectionImage RPC (hardware_rpc.py)
    # subscribers: task_executor (ToINSP task waiter 해제), container.insp_image_responder
    #              (현재는 task_executor orchestrator dispatch 미구현 보완용 fallback).
    INSP_IMAGE_UPLOADED = "INSP_IMAGE_UPLOADED"
    # ===== DB row trigger event bridge (SPEC: docs/db_event_bridge/SPEC.md) =====
    # PostgreSQL AFTER INSERT/UPDATE trigger → pg_notify('lifecycle_event', ...)
    # → DbEventListener → EventBridge.publish(DB_ROW_CHANGED, payload={table, op, row, ...})
    # 구독자가 payload.table 로 분기. Phase 1 은 generic, Phase 2+ 에서 table 별 specific event 추가 가능.
    DB_ROW_CHANGED = "DB_ROW_CHANGED"

## Task Executor
class TxnStat(str, Enum):
    """DB Schema (equip_task_txn / trans_task_txn) 의 txn_stat 컬럼과 매칭"""
    QUE = "QUE"
    PROC = "PROC"
    SUCC = "SUCC"
    FAIL = "FAIL"

## Orchestrator
class ResourceBindingPolicy(str, Enum):
    FREE = "FREE"
    REQUIRED = "REQUIRED"


## Resource (DDL res.res_type CHECK)
class ResourceType(str, Enum):
    MAT = "MAT"
    PAT = "PAT"
    CONV = "CONV"
    TAT = "TAT"


# TaskType → 어느 res_type 이 수행하는지 분류 (task_allocator 가 가용 자원 후보를 추리는 기준)
MAT_TASKS = frozenset({
    TaskType.MM,
    TaskType.POUR,
    TaskType.DM,
})

PAT_TASKS = frozenset({
    TaskType.PA_GP,
    TaskType.PA_DP,
    TaskType.PICK,
    TaskType.SHIP,
})

CONV_TASKS = frozenset({
    TaskType.PP,
    TaskType.INSP,
    TaskType.ToINSP,
    TaskType.ToPAWait,
})

TAT_TASKS = frozenset({
    TaskType.ToPP,
    TaskType.ToSTRG,
    TaskType.ToSHIP,
    TaskType.ToCHG,
})

# 실제 hw가 없어도 mock 으로 우회 가능한 task
BYPASSABLE_TASK_TYPES = frozenset({
    TaskType.PP,
    TaskType.ToINSP,
    TaskType.INSP,
    TaskType.ToPAWait,
})

# 완료 후에도 item 이 같은 res 에 계속 머무는 선행 task 들
ITEM_AFFINITY_PREDECESSORS = frozenset({
    TaskType.MM,
    TaskType.POUR,
    TaskType.PP,
    TaskType.ToINSP,
    TaskType.INSP,
})

# TAT transport task → src(상차) pose 매핑. dest pose 는 task_type 과 동일.
TRANS_SRC_POSE: dict[TaskType, str] = {
    TaskType.ToPP: "ToCAST",
    TaskType.ToSTRG: "ToINSP",
    TaskType.ToSHIP: "ToPICK",
}


def get_resource_binding_policy(task_type: TaskType) -> ResourceBindingPolicy:
    if task_type in {
        TaskType.POUR,
        TaskType.DM,
        TaskType.ToINSP,
        TaskType.INSP,
        TaskType.ToPAWait,
    }:
        return ResourceBindingPolicy.REQUIRED
    return ResourceBindingPolicy.FREE


def get_required_resource_type(
    task_type: TaskType | None,
    zone_nm: str | None = None,
) -> ResourceType | None:
    if task_type in TAT_TASKS:
        return ResourceType.TAT
    if task_type in CONV_TASKS:
        return ResourceType.CONV
    if task_type in PAT_TASKS:
        return ResourceType.PAT
    if task_type in MAT_TASKS:
        return ResourceType.MAT
    return None
