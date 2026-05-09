from __future__ import annotations

import json
import threading
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from services.core.adapters.ros2_runtime import Ros2Runtime


class PatAdapter:
    """Pat(적재&출고) Ros2 action client."""
    _ACTION_NAME = "/pat/execute_task"
    _STRG_COLUMNS = 6
    _STRG_SLOT_COUNT = 18
    _WAIT_SERVER_SEC = 5.0
    _RESULT_TIMEOUT_SEC = 300.0

    PICK_ACTION = "pat_pick_action"
    PLACE_ACTION = "pat_place_storage_action"
    RETRIEVE_ACTION = "pat_retrieve_action"
    DEFECT_ACTION = "pat_defect_drop_action"

    _ACTIONS = {PICK_ACTION, PLACE_ACTION, RETRIEVE_ACTION, DEFECT_ACTION}

    def __init__(self, ros2_runtime: Ros2Runtime | None = None) -> None:
        self._ros2_runtime = ros2_runtime
        self._node: Any | None = None
        self._client: Any | None = None
        self._goal_cls: Any | None = None
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
            from example_interfaces.action import Fibonacci
            from rclpy.action import ActionClient
            from rclpy.callback_groups import ReentrantCallbackGroup
            from rclpy.node import Node
        except ImportError:
            return

        self._node = Node("pat_adapter")
        self._goal_cls = Fibonacci.Goal
        self._client = ActionClient(
            self._node,
            Fibonacci,
            self._ACTION_NAME,
            callback_group=ReentrantCallbackGroup(),
        )
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
            return (False, f"unsupported_pat_command:{command}")

        try:
            params = json.loads(payload.decode("utf-8")) if payload else {}
        except json.JSONDecodeError:
            return (False, "invalid_json_payload")

        order = self._build_order(command, params)
        if order is None:
            return (False, f"{command}_requires_strg_loc_id")

        self.start()
        if not self._started or self._client is None or self._goal_cls is None:
            return (False, "pat_adapter_unavailable")

        return self._send_goal(order)

    def _send_goal(self, order: int) -> tuple[bool, str]:
        if not self._client.wait_for_server(timeout_sec=self._WAIT_SERVER_SEC):
            return (False, f"pat_action_server_unavailable:{self._ACTION_NAME}")

        goal = self._goal_cls()
        goal.order = order
        done = threading.Event()
        state: dict[str, Any] = {"success": False, "message": f"pat_order_{order}_not_completed"}

        def result_callback(future: Any) -> None:
            try:
                wrapped = future.result()
                sequence = list(getattr(wrapped.result, "sequence", []))
                state["success"] = bool(sequence and int(sequence[0]) == 1)
                state["message"] = f"pat_order_{order}_{'succeeded' if state['success'] else 'failed'}"
            except Exception as exc:
                state["success"] = False
                state["message"] = f"pat_result_exception:{exc}"
            finally:
                done.set()

        def goal_response_callback(future: Any) -> None:
            try:
                goal_handle = future.result()
            except Exception as exc:
                state["success"] = False
                state["message"] = f"pat_goal_exception:{exc}"
                done.set()
                return

            if goal_handle is None or not goal_handle.accepted:
                state["success"] = False
                state["message"] = f"pat_goal_rejected:{order}"
                done.set()
                return

            goal_handle.get_result_async().add_done_callback(result_callback)

        self._client.send_goal_async(goal).add_done_callback(goal_response_callback)
        if not done.wait(timeout=self._RESULT_TIMEOUT_SEC):
            return (False, f"pat_action_timeout:{order}")
        return (bool(state["success"]), str(state["message"]))

    def _build_order(self, command: str, params: dict[str, Any]) -> int | None:
        if command == self.PICK_ACTION:
            return 400
        if command == self.DEFECT_ACTION:
            return 300

        location = self._strg_location(params.get("strg_loc_id"))
        if location is None:
            return None
        floor, cell = location
        if command == self.PLACE_ACTION:
            return 100 + floor * 10 + cell
        if command == self.RETRIEVE_ACTION:
            return 200 + floor * 10 + cell
        return None

    @staticmethod
    def _strg_location(raw: Any | None) -> tuple[int, int] | None:
        if raw is None:
            return None
        try:
            loc_id = int(raw)
        except (TypeError, ValueError):
            return None
        if loc_id < 1 or loc_id > PatAdapter._STRG_SLOT_COUNT:
            return None
        floor = ((loc_id - 1) // PatAdapter._STRG_COLUMNS) + 1
        cell = ((loc_id - 1) % PatAdapter._STRG_COLUMNS) + 1
        return (floor, cell)

    def close(self) -> None:
        if self._node is None:
            return
        if self._ros2_runtime is not None:
            self._ros2_runtime.remove_node(self._node)
        self._node.destroy_node()
        self._node = None
        self._client = None
        self._goal_cls = None
        self._started = False
