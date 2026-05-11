from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from services.core.adapters.ros2_runtime import Ros2Runtime


# router 호환성: router 가 직접 import 하여 action 비교에 사용한다.
TAT_DOCK_ACTION = "dock_robot"


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


class TATAdapter:
    """AMR(이송) ROS2 action client."""

    _ACTIONS: Dict[str, ActionSpec] = {
        TAT_DOCK_ACTION: ActionSpec(
            name_fmt="/{robot_id}/dock_robot",
            action_attr="_dock_action",
            goal_builder=_build_dock_goal,
        ),
        # "undock_robot": ActionSpec(
        #     name_fmt="/{robot_id}/undock_robot",
        #     action_attr="_undock_action",
        #     goal_builder=_build_undock_goal,
        # ),
    }

    def __init__(self, ros2_runtime: Ros2Runtime | None = None) -> None:
        self._ros2_runtime = ros2_runtime
        self._node: Any | None = None
        self._callback_group: Any | None = None
        self._action_client_cls: Any | None = None
        self._goal_status_cls: Any | None = None
        self._dock_action: Any | None = None
        self._clients: dict[str, Any] = {}
        self._started = False

    @classmethod
    def supports(cls, action: str) -> bool:
        return action in cls._ACTIONS

    def start(self) -> None:
        if self._started:
            return
        if self._ros2_runtime is None:
            return

        self._ros2_runtime.start()
        if not self._ros2_runtime.started:
            return

        try:
            from action_msgs.msg import GoalStatus
            from nav2_msgs.action import DockRobot
            from rclpy.action import ActionClient
            from rclpy.callback_groups import ReentrantCallbackGroup
            from rclpy.node import Node
        except ImportError:
            return

        self._action_client_cls = ActionClient
        self._goal_status_cls = GoalStatus
        self._dock_action = DockRobot
        self._callback_group = ReentrantCallbackGroup()
        self._node = Node("tat_adapter")
        self._ros2_runtime.add_node(self._node)
        self._started = True

    def execute(
        self,
        _item_id: int,
        robot_id: str,
        command: str,
        payload: bytes,
    ) -> tuple[bool, str]:
        # 1) 입력 검증
        spec = self._ACTIONS.get(command)
        if spec is None:
            return (False, f"unsupported_tat_command:{command}")
        if not robot_id:
            return (False, "tat_robot_id_required")

        try:
            params = json.loads(payload.decode("utf-8")) if payload else {}
        except json.JSONDecodeError:
            return (False, "invalid_json_payload")

        # 2) 어댑터 시작 (idempotent)
        self.start()
        if not self._started or self._node is None:
            return (False, "tat_adapter_unavailable")

        # 3) goal 생성 (action_attr 으로 lazy-loaded 메시지 클래스 조회)
        action_cls = getattr(self, spec.action_attr, None)
        if action_cls is None:
            return (False, f"tat_action_class_unavailable:{command}")
        try:
            goal = spec.goal_builder(action_cls, params)
        except ValueError as exc:
            return (False, str(exc))

        wait_server_sec = float(params.get("wait_server_sec", 5.0))
        result_timeout_sec = float(params.get("result_timeout_sec", 300.0))
        action_name = spec.name_fmt.format(robot_id=robot_id)
        return self._send_goal(action_name, action_cls, goal, wait_server_sec, result_timeout_sec)

    def _send_goal(
        self,
        action_name: str,
        action_type: Any,
        goal: Any,
        wait_server_sec: float,
        result_timeout_sec: float,
    ) -> tuple[bool, str]:
        client = self._client(action_name, action_type)
        if not client.wait_for_server(timeout_sec=wait_server_sec):
            return (False, f"tat_action_server_unavailable:{action_name}")

        done = threading.Event()
        state: dict[str, Any] = {"success": False, "message": f"{action_name}_not_completed"}

        def result_callback(future: Any) -> None:
            try:
                status = future.result().status
                state["success"] = status == self._goal_status_cls.STATUS_SUCCEEDED
                state["message"] = (
                    f"{action_name}_succeeded"
                    if state["success"]
                    else f"{action_name}_failed:{status}"
                )
            except Exception as exc:
                state["success"] = False
                state["message"] = f"tat_result_exception:{exc}"
            finally:
                done.set()

        def goal_response_callback(future: Any) -> None:
            try:
                goal_handle = future.result()
            except Exception as exc:
                state["success"] = False
                state["message"] = f"tat_goal_exception:{exc}"
                done.set()
                return

            if goal_handle is None or not goal_handle.accepted:
                state["success"] = False
                state["message"] = f"tat_goal_rejected:{action_name}"
                done.set()
                return

            goal_handle.get_result_async().add_done_callback(result_callback)

        client.send_goal_async(goal).add_done_callback(goal_response_callback)
        if not done.wait(timeout=result_timeout_sec):
            return (False, f"tat_action_timeout:{action_name}")
        return (bool(state["success"]), str(state["message"]))

    def _client(self, action_name: str, action_type: Any) -> Any:
        if action_name not in self._clients:
            self._clients[action_name] = self._action_client_cls(
                self._node,
                action_type,
                action_name,
                callback_group=self._callback_group,
            )
        return self._clients[action_name]

    def close(self) -> None:
        if self._node is None:
            return
        if self._ros2_runtime is not None:
            self._ros2_runtime.remove_node(self._node)
        self._node.destroy_node()
        self._node = None
        self._clients.clear()
        self._started = False