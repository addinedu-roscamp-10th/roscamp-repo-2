from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from services.core.adapters.ros2_runtime import Ros2Runtime


@dataclass(frozen=True)
class _MatActionSpec:
    action_type_name: str
    requires_pattern_id: bool = False


class MatAdapter:
    """MAT(생산) ROS2 action client."""

    _ACTIONS: dict[str, _MatActionSpec] = {
        "pattern_pick_action": _MatActionSpec("PatternPick", requires_pattern_id=True),
        "die_stamp_action": _MatActionSpec("DieStamp"),
        "pattern_return_action": _MatActionSpec("PatternReturn", requires_pattern_id=True),
        "crucible_pick_action": _MatActionSpec("CruciblePick"),
        "metal_pour_action": _MatActionSpec("MetalPour"),
        "crucible_return_action": _MatActionSpec("CrucibleReturn"),
        "casting_pick_action": _MatActionSpec("CastingPick"),
        "tat_load_action": _MatActionSpec("TatLoad"),
        "return_to_base_action": _MatActionSpec("ReturnToBase"),
    }

    def __init__(self, ros2_runtime: Ros2Runtime | None = None) -> None:
        self._ros2_runtime = ros2_runtime
        self._node: Any | None = None
        self._callback_group: Any | None = None
        self._action_client_cls: Any | None = None
        self._action_types: dict[str, Any] = {}
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
            from cast_interfaces.action import (
                CastingPick,
                CruciblePick,
                CrucibleReturn,
                DieStamp,
                MetalPour,
                PatternPick,
                PatternReturn,
                ReturnToBase,
                TatLoad,
            )
            from rclpy.action import ActionClient
            from rclpy.callback_groups import ReentrantCallbackGroup
            from rclpy.node import Node
        except ImportError:
            return

        self._action_client_cls = ActionClient
        self._callback_group = ReentrantCallbackGroup()
        self._node = Node("mat_adapter")
        self._action_types = {
            "CastingPick": CastingPick,
            "CruciblePick": CruciblePick,
            "CrucibleReturn": CrucibleReturn,
            "DieStamp": DieStamp,
            "MetalPour": MetalPour,
            "PatternPick": PatternPick,
            "PatternReturn": PatternReturn,
            "ReturnToBase": ReturnToBase,
            "TatLoad": TatLoad,
        }
        self._ros2_runtime.add_node(self._node)
        self._started = True

    def execute(
        self,
        _item_id: int,
        _robot_id: str,
        command: str,
        payload: bytes,
    ) -> tuple[bool, str]:
        if command not in self._ACTIONS:
            return (False, f"unsupported_mat_command:{command}")

        try:
            params = json.loads(payload.decode("utf-8")) if payload else {}
        except json.JSONDecodeError:
            return (False, "invalid_json_payload")

        self.start()
        if not self._started or self._node is None:
            return (False, "mat_adapter_unavailable")

        spec = self._ACTIONS[command]
        action_type = self._action_types[spec.action_type_name]
        goal = action_type.Goal()
        if spec.requires_pattern_id:
            pattern_id = self._pattern_id(params)
            if pattern_id is None:
                return (False, f"{command}_requires_pattern_id")
            goal.pattern_id = pattern_id

        wait_server_sec = float(params.get("wait_server_sec", 5.0))
        result_timeout_sec = float(params.get("result_timeout_sec", 300.0))
        return self._send_goal(command, action_type, goal, wait_server_sec, result_timeout_sec)

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
            return (False, f"mat_action_server_unavailable:{action_name}")

        done = threading.Event()
        state: dict[str, Any] = {"success": False, "message": f"{action_name}_not_completed"}

        def result_callback(future: Any) -> None:
            try:
                wrapped = future.result()
                result = wrapped.result
                state["success"] = bool(getattr(result, "success", False))
                state["message"] = getattr(result, "message", "") or action_name
            except Exception as exc:
                state["success"] = False
                state["message"] = f"mat_result_exception:{exc}"
            finally:
                done.set()

        def goal_response_callback(future: Any) -> None:
            try:
                goal_handle = future.result()
            except Exception as exc:
                state["success"] = False
                state["message"] = f"mat_goal_exception:{exc}"
                done.set()
                return

            if goal_handle is None or not goal_handle.accepted:
                state["success"] = False
                state["message"] = f"mat_goal_rejected:{action_name}"
                done.set()
                return

            goal_handle.get_result_async().add_done_callback(result_callback)

        client.send_goal_async(goal).add_done_callback(goal_response_callback)
        if not done.wait(timeout=result_timeout_sec):
            return (False, f"mat_action_timeout:{action_name}")
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

    @staticmethod
    def _pattern_id(params: dict[str, Any]) -> int | None:
        raw = params.get("pattern_id", params.get("ptn_loc_id"))
        if raw is None:
            return None
        try:
            pattern_id = int(raw)
        except (TypeError, ValueError):
            return None
        if pattern_id not in {1, 2, 3}:
            return None
        return pattern_id

    def close(self) -> None:
        if self._node is None:
            return
        if self._ros2_runtime is not None:
            self._ros2_runtime.remove_node(self._node)
        self._node.destroy_node()
        self._node = None
        self._clients.clear()
        self._started = False
