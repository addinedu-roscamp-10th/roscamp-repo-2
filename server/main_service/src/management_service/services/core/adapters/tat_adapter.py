from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, TYPE_CHECKING

from services.contracts.models import AdapterResult
from services.core.adapters.ros2_adapter_base import BaseRos2Adapter

if TYPE_CHECKING:
    from services.core.adapters.ros2_runtime import Ros2Runtime


# router 호환성: router 가 직접 import 하여 action 비교에 사용한다.
TAT_DOCK_ACTION = "dock_robot"
TAT_UNDOCK_ACTION = "undock_robot"


# ─── action_type 별 매핑 ─────────────────────────────────────────
@dataclass(frozen=True)
class ActionSpec:
    """action 이름 하나가 요구하는 ROS2 action 정보."""
    name_fmt: str            # 액션 이름 템플릿 ({robot_id} 자리)
    action_attr: str         # 어댑터 인스턴스 속성 이름 (lazy import 결과 보관)
    goal_builder: Callable   # (action_cls, params: dict) → goal


def _build_dock_goal(action_cls: Any, params: dict) -> Any:
    """
    nav2_msgs/DockRobot goal 생성. 좌표는 로봇 내부 dock DB에서 조회.

    필수 params:
        pose_name: 로봇 내부에 등록된 dock id
    """
    pose_name = str(params.get("pose_name") or "")
    if not pose_name:
        raise ValueError("dock_robot requires params['pose_name']")

    goal = action_cls.Goal()
    goal.use_dock_id = True
    goal.dock_id = pose_name
    goal.dock_type = ""
    goal.navigate_to_staging_pose = True
    return goal


def _build_undock_goal(action_cls: Any, params: dict) -> Any:
    """nav2_msgs/UndockRobot goal 생성."""
    goal = action_cls.Goal()

    dock_type = str(params.get("dock_type") or "")
    if dock_type:
        goal.dock_type = dock_type

    max_undocking_time = params.get("max_undocking_time")
    if max_undocking_time is not None:
        goal.max_undocking_time = float(max_undocking_time)

    return goal


class TATAdapter(BaseRos2Adapter):
    """AMR(이송) ROS2 action client."""

    _ACTIONS: Dict[str, ActionSpec] = {
        TAT_DOCK_ACTION: ActionSpec(
            name_fmt="/{robot_id}/dock_robot",
            action_attr="_dock_action",
            goal_builder=_build_dock_goal,
        ),
        TAT_UNDOCK_ACTION: ActionSpec(
            name_fmt="/{robot_id}/undock_robot",
            action_attr="_undock_action",
            goal_builder=_build_undock_goal,
        ),
    }

    def __init__(self, runtime: Ros2Runtime | None = None) -> None:
        super().__init__(runtime=runtime)
        self._goal_status_cls: Any | None = None
        self._dock_action: Any | None = None
        self._undock_action: Any | None = None

    def start(self) -> None:
        if self._started:
            return
        if self._runtime is None:
            return

        try:
            from action_msgs.msg import GoalStatus
            from nav2_msgs.action import DockRobot, UndockRobot
            from rclpy.action import ActionClient
            from rclpy.node import Node
        except ImportError:
            return

        self._action_client_cls = ActionClient
        self._goal_status_cls = GoalStatus
        self._node_cls = Node
        self._dock_action = DockRobot
        self._undock_action = UndockRobot
        self._started = True

    async def send_command(
        self,
        res_id: str,
        action: str,
        params: dict[str, Any],
    ) -> AdapterResult:
        spec = self._ACTIONS.get(action)
        if spec is None:
            return AdapterResult(success=False, message=f"unsupported_tat_command:{action}")
        if not res_id:
            return AdapterResult(success=False, message="tat_robot_id_required")

        self.start()
        # _started == True 이면 start() 안에서 _goal_status_cls 등 모든 attr 가 한 번에 set 됨.
        if not self._started or self._goal_status_cls is None:
            return AdapterResult(success=False, message="tat_adapter_unavailable")
        succeeded_status = self._goal_status_cls.STATUS_SUCCEEDED

        if action == TAT_DOCK_ACTION and "aruco_num" in params:
            aruco_num = params["aruco_num"]
            try:
                from rcl_interfaces.srv import SetParameters
                from rcl_interfaces.msg import Parameter, ParameterValue, ParameterType

                req = SetParameters.Request()
                param = Parameter()
                param.name = "target_marker_id"
                param.value = ParameterValue(
                    type=ParameterType.PARAMETER_INTEGER,
                    integer_value=int(aruco_num)
                )
                req.parameters.append(param)

                srv_name = f"/{res_id}/aruco_marker_pose_node/set_parameters"
                await self._call_service_async(srv_name, SetParameters, req)
            except Exception as exc:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to set Aruco target marker ID on {res_id}: {exc}")

        action_cls = getattr(self, spec.action_attr)
        try:
            goal = spec.goal_builder(action_cls, params)
        except ValueError as exc:
            return AdapterResult(success=False, message=str(exc))

        action_name = spec.name_fmt.format(robot_id=res_id)
        client = self._get_or_create_client(action_name, action_cls)
        if client is None:
            return AdapterResult(success=False, message="tat_adapter_unavailable")

        wait_sec = float(params.get("wait_server_sec", 5.0))
        timeout_sec = float(params.get("result_timeout_sec", 300.0))

        def parse_result(wrapped: Any) -> tuple[bool, str]:
            status = wrapped.status
            success = status == succeeded_status
            message = (
                f"{action_name}_succeeded"
                if success
                else f"{action_name}_failed:{status}"
            )
            return (success, message)

        ok, msg = await self._send_single_goal_async(
            client,
            goal,
            parse_result,
            prefix="tat",
            action_name=action_name,
            wait_sec=wait_sec,
            timeout_sec=timeout_sec,
        )
        return AdapterResult(success=ok, message=msg)
