"""Robot status RPC methods with telemetry-first behavior."""

from __future__ import annotations

import logging

import management_pb2  # type: ignore

logger = logging.getLogger(__name__)


class RobotRpcMixin:
    """AMR telemetry/status RPCs."""

    def GetRobotStatus(self, request, context):
        entries = []
        robot_states = self.state_manager.get_all_robot_states()

        for state in robot_states:
            r_type = (
                management_pb2.ROBOT_TYPE_AMR
                if state.get("type") == "amr"
                else management_pb2.ROBOT_TYPE_COBOT
                if state.get("type") == "manipulator"
                else management_pb2.ROBOT_TYPE_UNSPECIFIED
            )
            entries.append(
                management_pb2.RobotStatusEntry(
                    id=state.get("id", ""),
                    type=r_type,
                    host=state.get("host", "ros2"),
                    status=state.get("status", "online"),
                    battery=float(state.get("battery", 0.0)),
                    voltage=float(state.get("voltage", 0.0)),
                    location=state.get("location", "-"),
                    task_state=int(state.get("task_state", 1) or 1),
                    task_id=str(state.get("task_id", "") or ""),
                    loaded_item=str(state.get("loaded_item", "") or ""),
                )
            )
        return management_pb2.GetRobotStatusResponse(robots=entries)

    def TransitionAmrState(self, request, context):
        logger.info(
            "TransitionAmrState ignored in telemetry-only mode: robot=%s new_state=%s",
            request.robot_id,
            request.new_state,
        )
        return management_pb2.TransitionAmrStateResponse(
            accepted=False,
            reason="deprecated: telemetry_only_mode",
        )
