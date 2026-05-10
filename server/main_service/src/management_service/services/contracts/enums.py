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
