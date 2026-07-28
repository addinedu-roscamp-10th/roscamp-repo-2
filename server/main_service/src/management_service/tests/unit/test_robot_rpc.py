from __future__ import annotations

from typing import Any
import pytest

from rpc.robot_rpc import RobotRpcMixin
from services.core.state_manager import StateManager


class DummyServicer(RobotRpcMixin):
    def __init__(self, state_manager: StateManager) -> None:
        self.state_manager = state_manager


def test_get_robot_status_from_state_manager() -> None:
    state_manager = StateManager(enable_persistence=False)
    # Seed 데이터를 override 하거나 추가 설정
    state_manager._res_list["TAT1"].update({
        "status": "IDLE",
        "battery_pct": 88,
        "x": 2.5,
        "y": 3.0,
        "task_id": "ToPP:100",
        "item_id": 42,
    })

    servicer = DummyServicer(state_manager=state_manager)
    resp = servicer.GetRobotStatus(request=None, context=None)

    assert resp is not None
    assert len(resp.robots) >= 1

    tat1_entry = next((r for r in resp.robots if r.id == "TAT1"), None)
    assert tat1_entry is not None
    assert tat1_entry.type == 1  # ROBOT_TYPE_AMR
    assert tat1_entry.battery == 88.0
    assert "x=2.50, y=3.00" in tat1_entry.location
    assert tat1_entry.task_id == "ToPP:100"
    assert tat1_entry.loaded_item == "42"
