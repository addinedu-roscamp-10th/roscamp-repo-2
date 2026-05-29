from __future__ import annotations

import asyncio

from services.core.adapters.ros2_adapter_base import _DomainSession
from services.core.adapters.tat_adapter import TATAdapter


class _RecordingTATAdapter(TATAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.requested_robot_ids: list[str] = []
        self.sent_action_names: list[str] = []

    def start(self) -> None:
        self._started = True
        self._goal_status_cls = type("GoalStatus", (), {"STATUS_SUCCEEDED": 4})
        self._dock_action = type("DockRobot", (), {"Goal": type("Goal", (), {})})
        self._undock_action = type("UndockRobot", (), {"Goal": type("Goal", (), {})})

    def _session_for(self, res_id: str) -> _DomainSession | None:
        self.requested_robot_ids.append(res_id)
        if res_id not in {"TAT2", "TAT4", "TAT5"}:
            return None
        return _DomainSession(runtime=object(), node=object())

    def _get_or_create_client(self, session, action_name, action_type):
        return object()

    async def _send_single_goal_async(
        self, client, goal, parse_result, *, action_name, **kwargs
    ) -> tuple[bool, str]:
        self.sent_action_names.append(action_name)
        return (True, action_name)


def test_dock_command_routes_to_dock_action_name() -> None:
    adapter = _RecordingTATAdapter()

    result = asyncio.run(
        adapter.send_command("TAT4", "dock_robot", {"pose_name": "ToCAST"})
    )

    assert result.success is True
    assert result.message == "/dock_robot"
    assert adapter.requested_robot_ids == ["TAT4"]
    assert adapter.sent_action_names == ["/dock_robot"]


def test_unknown_tat_robot_id_returns_unavailable() -> None:
    adapter = _RecordingTATAdapter()

    result = asyncio.run(
        adapter.send_command("TAT9", "dock_robot", {"pose_name": "ToCAST"})
    )

    assert result.success is False
    assert result.message == "tat_adapter_unavailable"
    assert adapter.requested_robot_ids == ["TAT9"]
    assert adapter.sent_action_names == []
