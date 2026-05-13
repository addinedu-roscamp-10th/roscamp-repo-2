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
