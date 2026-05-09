from __future__ import annotations

import json
import threading
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from services.core.adapters.ros2_runtime import Ros2Runtime


TAT_DOCK_ACTION = "dock_robot"

TAT_POSE_NAMES = {
    "ToCAST1",
    "ToPP1",
    "ToINSP",
    "ToSTRG1",
    "ToSTRG2",
    "ToSHIP",
    "ToCHG1",
}


class TATAdapter:
    """AMR(이송) Ros2 action client."""
    def __init__(
        self,
        ros2_runtime: Ros2Runtime | None = None,
    ) -> None:
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
        return action == TAT_DOCK_ACTION

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
        if command != TAT_DOCK_ACTION:
            return (False, f"unsupported_tat_command:{command}")
        if not robot_id:
            return (False, "tat_robot_id_required")

        try:
            params = json.loads(payload.decode("utf-8")) if payload else {}
        except json.JSONDecodeError:
            return (False, "invalid_json_payload")

        self.start()
        if not self._started or self._node is None:
            return (False, "tat_adapter_unavailable")

        pose_name = str(params.get("pose_name") or "")
        if pose_name not in TAT_POSE_NAMES:
            return (False, f"unsupported_tat_pose:{pose_name}")

        wait_server_sec = float(params.get("wait_server_sec", 5.0))
        result_timeout_sec = float(params.get("result_timeout_sec", 300.0))
        goal = self._build_goal(pose_name)
        action_name = f"/{robot_id}/dock_robot"
        return self._send_goal(action_name, self._dock_action, goal, wait_server_sec, result_timeout_sec)

    def _build_goal(self, pose_name: str) -> Any:
        goal = self._dock_action.Goal()
        goal.use_dock_id = True
        goal.dock_id = pose_name
        goal.dock_type = ""
        goal.navigate_to_staging_pose = True
        return goal

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
