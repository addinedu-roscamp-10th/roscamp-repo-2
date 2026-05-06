from __future__ import annotations
import math
from typing import TYPE_CHECKING

from services.contracts.enums import EquipTaskType, TransTaskType, TransferPoint
from services.contracts.models import (
    AllocateTaskInput,
    AllocateTaskResult,
    AmrLocationResult,
    AssignTaskRobotInput,
)

if TYPE_CHECKING:
    from services.contracts.protocols import IStateManager

# 각 경유지 별 좌표 (tat_nav_pose_master 기준)
TRANSFER_POINT_COORDS: dict[TransferPoint, tuple[float, float]] = {
    TransferPoint.CAST_OUT: (-0.256, 0.20),      # ToCAST
    TransferPoint.PP_CONV_END: (-0.447, -1.05),  # ToPP
    TransferPoint.STRG_IN: (-0.10, -0.465),      # ToSTRG
    TransferPoint.CHG_IN: (0.044, 0.095),        # ToCHG1
}

STORAGE_AND_SHIPPING_TASKS = {
    EquipTaskType.PICK,
    EquipTaskType.SHIP,
    "PICK",
    "SHIP",
}


class TaskAllocator:
    """각 task를 어떤 res가 수행할지 결정하고 반환."""

    def __init__(self, state_manager: IStateManager):
        self.state_manager = state_manager

    # task별 필요한 res 분류 반환 (RA, TAT, CONV)
    def _get_required_resource_type(
        self,
        task_type: str | EquipTaskType | TransTaskType | None,
        zone_nm: str | None = None,
    ) -> str:
        if isinstance(task_type, TransTaskType) or task_type in [t.value for t in TransTaskType]:
            return "TAT"

        if task_type == EquipTaskType.ToINSP or task_type == "ToINSP" or zone_nm == "INSP":
            return "CONV"

        if zone_nm == "STRG" or task_type in STORAGE_AND_SHIPPING_TASKS:
            return "RA_STRG"

        return "RA_CAST"

    async def update_task_allocation(self, task: AllocateTaskInput, robot_id: str) -> None:
        assign_input = AssignTaskRobotInput(
            task_id=task.task_id,
            item_id=task.item_id,
            robot_id=robot_id,
        )
        self.state_manager.update_task_allocation(assign_input)

    # 각 src의 좌표 반환
    def _get_transfer_point(
        self,
        task_type: str | EquipTaskType | TransTaskType | None,
    ) -> TransferPoint | None:
        task_to_transfer_point = {
            TransTaskType.ToPP: TransferPoint.CAST_OUT,
            TransTaskType.ToSTRG: TransferPoint.PP_CONV_END,
            TransTaskType.ToSHIP: TransferPoint.STRG_IN,
            TransTaskType.ToCHG: TransferPoint.CHG_IN,
            "ToPP": TransferPoint.CAST_OUT,
            "ToSTRG": TransferPoint.PP_CONV_END,
            "ToSHIP": TransferPoint.STRG_IN,
            "ToCHG": TransferPoint.CHG_IN,
        }
        return task_to_transfer_point.get(task_type)

    # 각 TAT와 src 간의 거리 계산
    def _get_distance_to_transfer_point(
        self,
        amr_location: AmrLocationResult,
        transfer_point: TransferPoint,
    ) -> float:
        target_x, target_y = TRANSFER_POINT_COORDS[transfer_point]
        return math.dist((amr_location.x, amr_location.y), (target_x, target_y))

    # 리소스 선택 함수
    def _select_resource(
        self,
        available_resources: list[str],
        task_type: str | EquipTaskType | TransTaskType | None,
        zone_nm: str | None = None,
        amr_locations: list[AmrLocationResult] | None = None,
    ) -> str | None:
        is_trans = isinstance(task_type, TransTaskType) or task_type in [t.value for t in TransTaskType]
        
        # 1. TAT(AMR) 선택 로직: 가장 가까운 로봇 선택
        if amr_locations and is_trans:
            transfer_point = self._get_transfer_point(task_type)
            if transfer_point is not None: 
                available_amr_locations = [
                    amr_location
                    for amr_location in amr_locations
                    if amr_location.res_id in available_resources
                ]
                if available_amr_locations:
                    nearest_amr = min(
                        available_amr_locations,
                        key=lambda amr_location: self._get_distance_to_transfer_point(
                            amr_location,
                            transfer_point,
                        ),
                    )
                    return nearest_amr.res_id
            return available_resources[0] if available_resources else None

        return available_resources[0] if available_resources else None

    # 메인함수
    async def allocate(
        self,
        task: AllocateTaskInput,
    ) -> AllocateTaskResult:
        # 요청된 task에 대한 res 타입 결정
        req_res_type = self._get_required_resource_type(task.task_type, task.zone_nm)
        
        # 가용 리소스 조회
        available_resources = await self.state_manager.get_available_resources(req_res_type)

        if not available_resources:
            return AllocateTaskResult(success=False, reason="no_available_resource")


        # POUR, DM은 직전 작업을 수행한 로봇이 이어서 수행해야 함
        sticky_tasks = {EquipTaskType.POUR, EquipTaskType.DM, "POUR", "DM"}
        if task.task_type in sticky_tasks and task.last_res_id:
            if task.last_res_id in available_resources:
                return AllocateTaskResult(success=True, robot_id=task.last_res_id)
            else:
                return AllocateTaskResult(success=False, reason=f"sticky_robot_{task.last_res_id}_not_available")

        amr_locations: list[AmrLocationResult] | None = None
        if req_res_type == "TAT":
            amr_locations = await self.state_manager.get_amr_locations()

        # 4. 최적 리소스 선택 (일반적인 경우)
        selected_res_id = self._select_resource(
            available_resources,
            task.task_type,
            task.zone_nm,
            amr_locations,
        )

        if not selected_res_id:
            return AllocateTaskResult(success=False, reason="resource_not_found")

        return AllocateTaskResult(
            success=True,
            robot_id=selected_res_id,
        )
