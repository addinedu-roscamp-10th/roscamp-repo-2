"""ConvAdapter 통합 — CONV_ALLOW_MOVE 수신 시 command_queue("RUN") enqueue 검증.

ToPAWait task step 3 (CONV_ALLOW_MOVE) 가 conv_adapter 를 호출하면
command_queue 에 ConveyorCmd("RUN") 이 enqueue 되어야 한다.
이 명령은 hardware_rpc.WatchConveyorCommands stream 으로 Jetson 에 전달되어
ESP32 Serial "RUN\\n" → ST_STOPPED → ST_POST_RUN (4 s) 전이를 트리거.

AMR 도착 (ToPAWait step 2 의 WAIT_SUBTASK_COMPLETED "tostrg") 이후에만 본 step 이
실행되므로 컨베이어 가동 시점이 안전.

2026-05-14 변경: 기존 EventBridge.INSP_COMPLETED publish 우회 경로를
WatchConveyorCommands 단일 채널로 통합. PP_DONE 1 차 "start" 명령과 동일 채널.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _drain_command_queue():
    """매 테스트 시작/종료 시 전역 command_queue 비우기."""
    from services.legacy.command_queue import queue as command_queue

    while command_queue.size() > 0:
        command_queue.wait_next(robot_id_filter=None, timeout=0.01)
    yield
    while command_queue.size() > 0:
        command_queue.wait_next(robot_id_filter=None, timeout=0.01)


def test_conv_adapter_enqueues_run_on_allow_move() -> None:
    from services.core.adapters.conv_adapter import ConvAdapter
    from services.legacy.command_queue import queue as command_queue

    adapter = ConvAdapter()
    payload = json.dumps({"duration_sec": 4.0}).encode("utf-8")
    ok, msg = adapter.execute(
        item_id=42, robot_id="CONV1", command="CONV_ALLOW_MOVE", payload=payload
    )

    assert ok is True
    assert msg == "conv_allow_move_accepted"

    cmd = command_queue.wait_next(robot_id_filter=None, timeout=1.0)
    assert cmd is not None, "ConveyorCmd 가 enqueue 되지 않았다"
    assert cmd.robot_id == "CONV1"
    assert cmd.command == "RUN"
    assert cmd.item_id == 42
    assert cmd.issued_by == "conv_adapter.CONV_ALLOW_MOVE"

    cmd_payload = json.loads(cmd.payload.decode("utf-8")) if cmd.payload else {}
    assert cmd_payload.get("duration_sec") == 4.0
    assert cmd_payload.get("source") == "conv_adapter.CONV_ALLOW_MOVE"


def test_conv_adapter_enqueues_run_without_duration() -> None:
    """duration_sec 미지정 — enqueue 자체는 성공, payload 에 duration_sec 키 없음."""
    from services.core.adapters.conv_adapter import ConvAdapter
    from services.legacy.command_queue import queue as command_queue

    adapter = ConvAdapter()
    ok, msg = adapter.execute(
        item_id=7, robot_id="CONV1", command="CONV_ALLOW_MOVE", payload=b"{}"
    )
    assert ok is True
    assert msg == "conv_allow_move_accepted"

    cmd = command_queue.wait_next(robot_id_filter=None, timeout=1.0)
    assert cmd is not None
    assert cmd.command == "RUN"
    cmd_payload = json.loads(cmd.payload.decode("utf-8")) if cmd.payload else {}
    assert "duration_sec" not in cmd_payload


def test_conv_adapter_rejects_invalid_command() -> None:
    from services.core.adapters.conv_adapter import ConvAdapter
    from services.legacy.command_queue import queue as command_queue

    adapter = ConvAdapter()
    ok, msg = adapter.execute(
        item_id=1, robot_id="CONV1", command="BOGUS", payload=b"{}"
    )
    assert ok is False
    assert "unsupported_conv_command" in msg

    cmd = command_queue.wait_next(robot_id_filter=None, timeout=0.1)
    assert cmd is None, "거부된 명령에 대해 enqueue 되면 안 됨"


def test_conv_adapter_rejects_invalid_payload() -> None:
    from services.core.adapters.conv_adapter import ConvAdapter
    from services.legacy.command_queue import queue as command_queue

    adapter = ConvAdapter()
    ok, msg = adapter.execute(
        item_id=1, robot_id="CONV1", command="CONV_ALLOW_MOVE", payload=b"not-json"
    )
    assert ok is False
    assert msg == "invalid_json_payload"

    assert command_queue.wait_next(robot_id_filter=None, timeout=0.1) is None


def test_conv_adapter_rejects_negative_duration() -> None:
    from services.core.adapters.conv_adapter import ConvAdapter
    from services.legacy.command_queue import queue as command_queue

    adapter = ConvAdapter()
    ok, msg = adapter.execute(
        item_id=1,
        robot_id="CONV1",
        command="CONV_ALLOW_MOVE",
        payload=json.dumps({"duration_sec": -1.0}).encode("utf-8"),
    )
    assert ok is False
    assert msg == "invalid_duration_sec"

    assert command_queue.wait_next(robot_id_filter=None, timeout=0.1) is None


def test_conv_adapter_requires_robot_id() -> None:
    from services.core.adapters.conv_adapter import ConvAdapter
    from services.legacy.command_queue import queue as command_queue

    adapter = ConvAdapter()
    ok, msg = adapter.execute(
        item_id=1, robot_id="", command="CONV_ALLOW_MOVE", payload=b"{}"
    )
    assert ok is False
    assert msg == "conv_robot_id_required"

    assert command_queue.wait_next(robot_id_filter=None, timeout=0.1) is None
