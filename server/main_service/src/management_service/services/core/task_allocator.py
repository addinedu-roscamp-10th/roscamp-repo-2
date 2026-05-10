from __future__ import annotations
import logging
import math
from typing import TYPE_CHECKING, Sequence

from services.contracts.enums import TaskType
from services.contracts.models import (
    AllocateTaskInput,
    AllocateTaskResult,
    AllocateTaskResInput,
    AmrRuntimeState,
)

if TYPE_CHECKING:
    from services.contracts.protocols import IStateManager

logger = logging.getLogger(__name__)

# 각 상차 시작 pose 좌표 (tat_nav_pose_master 기준)
TRANSFER_POINT_COORDS: dict[str, tuple[float, float]] = {
    "ToCAST": (-0.256, 0.20),
    "ToINSP": (-0.67, -0.10),
    "ToPICK": (-0.223, -0.465),
}

TASK_BATTERY_THRESHOLDS = {
    TaskType.ToPP: 30, # src-dest 기준으로 dist가 가장 멈
    TaskType.ToSTRG: 20, # dist가 가장 가까움
    TaskType.ToSHIP: 25, # 중간 dist
}

SRC_POSE = {
    TaskType.ToPP: "ToCAST",
    TaskType.ToSTRG: "ToINSP",
    TaskType.ToSHIP: "ToPICK",
}


MAT_TASKS = {
    TaskType.MM,
    TaskType.POUR,
    TaskType.DM,
}

PAT_TASKS = {
    TaskType.PA_GP,
    TaskType.PA_DP,
    TaskType.PICK,
    TaskType.SHIP,
}

ON_CONVEYOR_TASKS = {
    TaskType.PP,
    TaskType.INSP,
    TaskType.ToINSP,
    TaskType.ToPAWait,
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

    if task_type in ON_CONVEYOR_TASKS:
        return "CONV"

    if task_type in PAT_TASKS:
        return "PAT"

    if task_type in MAT_TASKS:
        return "MAT"

    return None


class TaskAllocator:
    """각 task를 어떤 res가 수행할지 결정하고 반환."""

    DEFAULT_BATTERY_THRESHOLD = 30  # task에 설정된 임계치 없을 때

    def __init__(self, state_manager: IStateManager):
        self.state_manager = state_manager

    async def _update_task_allocation(self, task: AllocateTaskInput, res_id: str) -> None:
        assign_input = AllocateTaskResInput(
            task_id=task.task_id,
            item_id=task.item_id,
            res_id=res_id,
        )
        await self.state_manager.update_task_allocation(assign_input)

    # task별 src pose 이름 반환
    def _get_src_pose_name(
        self,
        task_type: TaskType | None,
    ) -> str | None:
        return SRC_POSE.get(task_type)

    # 각 TAT와 src 간의 dist 계산
    def _get_distance_to_source_pose(
        self,
        amr_location: AmrRuntimeState,
        src_pose_name: str,
    ) -> float:
        target_x, target_y = TRANSFER_POINT_COORDS[src_pose_name]
        return math.dist((amr_location.x, amr_location.y), (target_x, target_y))

    # res 선택 함수
    def _select_resource(
        self,
        available_resources: list[str],
        task_type: TaskType | None,
        zone_nm: str | None = None,
        amr_stats: Sequence[AmrRuntimeState] = (),
    ) -> str | None:
        # AMR일 때 bat 및 dist를 점수 기반으로 계산해서 할당
        if amr_stats:
            req_bat = TASK_BATTERY_THRESHOLDS.get(task_type, self.DEFAULT_BATTERY_THRESHOLD)
            
            # bat 임계치를 만족하는 후보 필터링
            candidates: list[AmrRuntimeState] = [
                amr for amr in amr_stats
                if amr.res_id in available_resources and amr.bat_pct >= req_bat
            ]

            # 모든 AMR이 bat_threshold 이하면 할당 실패
            if not candidates:
                return None

            # 1대 밖에 없으면 바로 반환
            if len(candidates) == 1:
                return candidates[0].res_id

            # 2대 이상인 경우 점수 계산
            src_pose_name = self._get_src_pose_name(task_type)
            if not src_pose_name:
                logger.warning(
                    "src pose mapping missing: task_type=%s",
                    task_type,
                )
                return None

            candidate_stats = []
            for amr in candidates:
                dist = self._get_distance_to_source_pose(amr, src_pose_name)
                remain_bat = amr.bat_pct - req_bat  # 필요한 bat을 뺀 잔여 bat
                candidate_stats.append({
                    "res_id": amr.res_id,
                    "dist": dist,
                    "remain_bat": remain_bat
                })

            # 정규화 후 최종 점수 계산(가까울수록, 남는 bat가 적을수록 높은 점수)
            min_dist = min(s["dist"] for s in candidate_stats)
            max_dist = max(s["dist"] for s in candidate_stats)
            min_remain_bat = min(s["remain_bat"] for s in candidate_stats)
            max_remain_bat = max(s["remain_bat"] for s in candidate_stats)

            best_amr_id = None
            highest_score = -1.0

            for stats in candidate_stats:
                # dist 점수 (가까울수록 높음)
                norm_dist = (
                    (stats["dist"] - min_dist) / (max_dist - min_dist)
                    if max_dist > min_dist else 0.0
                )
                dist_score = 1.0 - norm_dist

                # bat 점수 (잔여 bat가 적을수록 높음 -> bat 많은 기기를 보존)
                norm_remain_bat = (
                    (stats["remain_bat"] - min_remain_bat) / (max_remain_bat - min_remain_bat)
                    if max_remain_bat > min_remain_bat else 0.0
                )
                bat_score = 1.0 - norm_remain_bat

                total_score = (0.7 * dist_score) + (0.3 * bat_score) # dist 70, bat 30

                if total_score > highest_score:
                    highest_score = total_score
                    best_amr_id = stats["res_id"]

            return best_amr_id

        # AMR이 아닐 경우에는 첫 번째 res 할당
        return available_resources[0]

    # 메인함수
    async def allocate(
        self,
        task: AllocateTaskInput,
    ) -> AllocateTaskResult:
        # 입력에 타입이 없으면 allocator가 req_res_type을 계산
        req_res_type = task.req_res_type or _get_required_resource_type(task.task_type, task.zone_nm)

        # req_res_id가 들어오면 반드시 그 res가 할당되도록
        if task.req_res_id:
            await self._update_task_allocation(task, task.req_res_id)
            return AllocateTaskResult(
                success=True,
                req_res_type=req_res_type,
                res_id=task.req_res_id,
            )

        # 가용 리소스 조회
        available_resources = await self.state_manager.get_available_resources(req_res_type)

        if not available_resources:
            return AllocateTaskResult(
                success=False,
                req_res_type=req_res_type,
                reason="no_available_resource",
            )

        amr_stats: list[AmrRuntimeState] = []
        if req_res_type == "TAT":
            amr_stats = await self.state_manager.get_amr_stats()

        # 최적 리소스 선택
        selected_res_id = self._select_resource(
            available_resources,
            task.task_type,
            task.zone_nm,
            amr_stats,
        )

        # 가용 가능 AMR이 있지만, bat 임계치를 넘지 못하는 경우
        if not selected_res_id:
            return AllocateTaskResult(
                success=False,
                req_res_type=req_res_type,
                reason="no_suitable_resource",
            )

        await self._update_task_allocation(task, selected_res_id)
        return AllocateTaskResult(
            success=True,
            req_res_type=req_res_type,
            res_id=selected_res_id,
        )
