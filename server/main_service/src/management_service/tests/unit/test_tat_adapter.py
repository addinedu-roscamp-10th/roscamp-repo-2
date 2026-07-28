from __future__ import annotations

import asyncio

from services.core.adapters.tat_adapter import TATAdapter


class _RecordingTATAdapter(TATAdapter):
    def __init__(self, *, allow_robots: set[str] | None = None) -> None:
        super().__init__()
        self.requested_action_names: list[str] = []
        self.sent_action_names: list[str] = []
        self._allow_robots = allow_robots

    def start(self) -> None:
        self._started = True
        self._goal_status_cls = type("GoalStatus", (), {"STATUS_SUCCEEDED": 4})
        self._dock_action = type("DockRobot", (), {"Goal": type("Goal", (), {})})
        self._undock_action = type("UndockRobot", (), {"Goal": type("Goal", (), {})})

    def _get_or_create_client(self, action_name: str, action_type):
        self.requested_action_names.append(action_name)
        if self._allow_robots is not None:
            # action_name 의 첫 segment (예: "/TAT4") 가 허용 목록에 있어야 client 발급
            robot_id = action_name.lstrip("/").split("/", 1)[0]
            if robot_id not in self._allow_robots:
                return None
        return object()

    async def _send_single_goal_async(
        self, client, goal, parse_result, *, action_name, **kwargs
    ) -> tuple[bool, str]:
        self.sent_action_names.append(action_name)
        return (True, action_name)


def test_dock_command_routes_to_namespaced_action_name() -> None:
    adapter = _RecordingTATAdapter(allow_robots={"TAT2", "TAT4", "TAT5"})

    result = asyncio.run(
        adapter.send_command("TAT4", "dock_robot", {"pose_name": "ToCAST"})
    )

    assert result.success is True
    assert result.message == "/TAT4/dock_robot"
    assert adapter.requested_action_names == ["/TAT4/dock_robot"]
    assert adapter.sent_action_names == ["/TAT4/dock_robot"]


def test_undock_command_uses_robot_id_namespace() -> None:
    adapter = _RecordingTATAdapter(allow_robots={"TAT2"})

    result = asyncio.run(adapter.send_command("TAT2", "undock_robot", {}))

    assert result.success is True
    assert result.message == "/TAT2/undock_robot"


def test_unknown_tat_robot_id_returns_unavailable() -> None:
    adapter = _RecordingTATAdapter(allow_robots={"TAT2", "TAT4", "TAT5"})

    result = asyncio.run(
        adapter.send_command("TAT9", "dock_robot", {"pose_name": "ToCAST"})
    )

    assert result.success is False
    assert result.message == "tat_adapter_unavailable"
    assert adapter.requested_action_names == ["/TAT9/dock_robot"]
    assert adapter.sent_action_names == []
