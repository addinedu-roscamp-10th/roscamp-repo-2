from __future__ import annotations
import math
from typing import TYPE_CHECKING

from services.contracts.enums import TaskType, TransferPoint
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
    TaskType.PA_GP,
    TaskType.PA_DP,
    TaskType.PICK,
    TaskType.SHIP,
}

CONVEYOR_TASKS = {
    TaskType.PP,
    TaskType.INSP,
    TaskType.ToINSP,
    TaskType.ToWaitPA,
}

TRANSPORT_TASKS = {
    TaskType.ToPP,
    TaskType.ToSTRG,
    TaskType.ToSHIP,
    TaskType.ToCHG,
}

def _get_required_resource_type(
    task_type: TaskType | None,
    zone_nm: str | None = None,
) -> str:
    if task_type in TRANSPORT_TASKS:
        return "TAT"

    if task_type in CONVEYOR_TASKS or zone_nm == "INSP":
        return "CONV"

    if zone_nm == "STRG" or task_type in STORAGE_AND_SHIPPING_TASKS:
        return "RA_STRG"

    return "RA_CAST"


class TaskAllocator:
    """각 task를 어떤 res가 수행할지 결정하고 반환."""

    def __init__(self, state_manager: IStateManager):
        self.state_manager = state_manager

    async def _update_task_allocation(self, task: AllocateTaskInput, robot_id: str) -> None:
        assign_input = AssignTaskRobotInput(
            task_id=task.task_id,
            item_id=task.item_id,
            robot_id=robot_id,
        )
        await self.state_manager.update_task_allocation(assign_input)

    # 각 src의 좌표 반환
    def _get_transfer_point(
        self,
        task_type: TaskType | None,
    ) -> TransferPoint | None:
        task_to_transfer_point = {
            TaskType.ToPP: TransferPoint.CAST_OUT,
            TaskType.ToSTRG: TransferPoint.PP_CONV_END,
            TaskType.ToSHIP: TransferPoint.STRG_IN,
            TaskType.ToCHG: TransferPoint.CHG_IN,
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
        task_type: TaskType | None,
        zone_nm: str | None = None,
        amr_locations: list[AmrLocationResult] | None = None,
    ) -> str | None:
        is_trans = task_type in TRANSPORT_TASKS
        
        # TAT(AMR) 선택 로직: 가장 가까운 로봇 선택
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
        # 입력에 타입이 없으면 allocator가 최종 req_res_type을 해석한다.
        req_res_type = task.req_res_type or _get_required_resource_type(task.task_type, task.zone_nm)

        # req_res_id가 들어오면 반드시 그 로봇이 할당되도록
        if task.req_res_id:
            await self._update_task_allocation(task, task.req_res_id)
            return AllocateTaskResult(
                success=True,
                req_res_type=req_res_type,
                robot_id=task.req_res_id,
            )

        # 가용 리소스 조회
        available_resources = await self.state_manager.get_available_resources(req_res_type)

        if not available_resources:
            return AllocateTaskResult(
                success=False,
                req_res_type=req_res_type,
                reason="no_available_resource",
            )

        amr_locations: list[AmrLocationResult] | None = None
        if req_res_type == "TAT":
            amr_locations = await self.state_manager.get_amr_locations()

        # 최적 리소스 선택
        selected_res_id = self._select_resource(
            available_resources,
            task.task_type,
            task.zone_nm,
            amr_locations,
        )

        await self._update_task_allocation(task, selected_res_id)
        return AllocateTaskResult(
            success=True,
            req_res_type=req_res_type,
            robot_id=selected_res_id,
        )
