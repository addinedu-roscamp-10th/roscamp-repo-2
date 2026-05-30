from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from services.contracts.models import AdapterResult
from services.core.adapters.ros2_adapter_base import BaseRos2Adapter

if TYPE_CHECKING:
    from services.core.adapters.ros2_runtime import Ros2Runtime


@dataclass(frozen=True)
class _MatActionSpec:
    action_type_name: str
    requires_pattern_id: bool = False


class MatAdapter(BaseRos2Adapter):
    """MAT(생산) ROS2 action client."""

    _ACTION_NAME_FMT = "/{robot_id}/{action}"

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

    def __init__(self, runtime: Ros2Runtime | None = None) -> None:
        super().__init__(runtime=runtime)
        self._action_types: dict[str, Any] = {}

    def start(self) -> None:
        if self._started:
            return
        if self._runtime is None:
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
            from rclpy.node import Node
        except ImportError:
            return

        self._action_client_cls = ActionClient
        self._node_cls = Node
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
        self._started = True

    async def send_command(
        self,
        res_id: str,
        action: str,
        params: dict[str, Any],
    ) -> AdapterResult:
        return AdapterResult(success=True, message=action)

    async def send_command(
        self,
        res_id: str,
        action: str,
        params: dict[str, Any],
    ) -> AdapterResult:
        if action not in self._ACTIONS:
            return AdapterResult(success=False, message=f"unsupported_mat_command:{action}")
        if not res_id:
            return AdapterResult(success=False, message="mat_robot_id_required")

        self.start()
        if not self._started:
            return AdapterResult(success=False, message="mat_adapter_unavailable")

        spec = self._ACTIONS[action]
        action_type = self._action_types[spec.action_type_name]
        goal = action_type.Goal()
        if spec.requires_pattern_id:
            pattern_id = self._pattern_id(params)
            if pattern_id is None:
                return AdapterResult(success=False, message=f"{action}_requires_pattern_id")
            goal.pattern_id = pattern_id

        action_name = self._ACTION_NAME_FMT.format(robot_id=res_id, action=action)
        client = self._get_or_create_client(action_name, action_type)
        if client is None:
            return AdapterResult(success=False, message="mat_adapter_unavailable")

        wait_sec = float(params.get("wait_server_sec", 5.0))
        timeout_sec = float(params.get("result_timeout_sec", 300.0))

        def parse_result(wrapped: Any) -> tuple[bool, str]:
            result = wrapped.result
            success = bool(getattr(result, "success", False))
            message = getattr(result, "message", "") or action
            return (success, message)

        ok, msg = await self._send_single_goal_async(
            client,
            goal,
            parse_result,
            prefix="mat",
            action_name=action_name,
            wait_sec=wait_sec,
            timeout_sec=timeout_sec,
        )
        return AdapterResult(success=ok, message=msg)

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
