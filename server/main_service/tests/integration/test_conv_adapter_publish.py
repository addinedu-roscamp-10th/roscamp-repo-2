"""ConvAdapter 통합 — CONV_ALLOW_MOVE 수신 시 INSP_COMPLETED publish 검증.

ToPAWait task step 3 (CONV_ALLOW_MOVE) 가 conv_adapter 를 호출하면
EventBridge 에 INSP_COMPLETED 이벤트가 publish 되어야 한다 (PR #9 의 EventGateway
servicer 가 이를 Jetson WatchEvents 로 forward → ESP32 컨베이어 RUN).

AMR 도착 (ToPAWait step 2 의 WAIT_SUBTASK_COMPLETED "tostrg") 이후에만 본 step 이
실행되므로 컨베이어 가동 시점이 안전.
"""

from __future__ import annotations

import json


def test_conv_adapter_publishes_insp_completed_on_allow_move() -> None:
    from services.contracts.enums import EventType
    from services.contracts.models import Event
    from services.core.adapters.conv_adapter import ConvAdapter
    from services.core.event_bridge import EventBridgeImpl

    event_bridge = EventBridgeImpl()
    captured: list[Event] = []
    event_bridge.subscribe(
        EventType.INSP_COMPLETED,
        lambda evt: captured.append(evt),
        subscriber_name="test_spy",
    )

    adapter = ConvAdapter(event_bridge=event_bridge)
    payload = json.dumps({"duration_sec": 4.0}).encode("utf-8")
    ok, msg = adapter.execute(item_id=42, robot_id="CONV1", command="CONV_ALLOW_MOVE", payload=payload)

    assert ok is True
    assert msg == "conv_allow_move_accepted"
    assert len(captured) == 1
    event = captured[0]
    assert event.event_type == EventType.INSP_COMPLETED
    assert event.item_id == 42
    assert event.res_id == "CONV1"
    assert event.payload["item_id"] == 42
    assert event.payload["res_id"] == "CONV1"
    assert event.payload["duration_sec"] == 4.0
    assert event.payload["source"] == "conv_adapter.CONV_ALLOW_MOVE"


def test_conv_adapter_skips_publish_without_event_bridge() -> None:
    """event_bridge 미주입 시 publish skip — adapter 는 여전히 success 반환."""
    from services.core.adapters.conv_adapter import ConvAdapter

    adapter = ConvAdapter()  # event_bridge=None
    ok, msg = adapter.execute(
        item_id=1, robot_id="CONV1", command="CONV_ALLOW_MOVE", payload=b"{}"
    )
    assert ok is True
    assert msg == "conv_allow_move_accepted"


def test_conv_adapter_rejects_invalid_command() -> None:
    from services.contracts.enums import EventType
    from services.contracts.models import Event
    from services.core.adapters.conv_adapter import ConvAdapter
    from services.core.event_bridge import EventBridgeImpl

    event_bridge = EventBridgeImpl()
    captured: list[Event] = []
    event_bridge.subscribe(
        EventType.INSP_COMPLETED, lambda evt: captured.append(evt), subscriber_name="spy"
    )
    adapter = ConvAdapter(event_bridge=event_bridge)

    ok, msg = adapter.execute(item_id=1, robot_id="CONV1", command="BOGUS", payload=b"{}")
    assert ok is False
    assert "unsupported_conv_command" in msg
    assert captured == []  # publish 안 함


def test_conv_adapter_rejects_invalid_payload() -> None:
    from services.core.adapters.conv_adapter import ConvAdapter

    adapter = ConvAdapter()
    ok, msg = adapter.execute(
        item_id=1, robot_id="CONV1", command="CONV_ALLOW_MOVE", payload=b"not-json"
    )
    assert ok is False
    assert msg == "invalid_json_payload"


def test_conv_adapter_rejects_negative_duration() -> None:
    from services.core.adapters.conv_adapter import ConvAdapter

    adapter = ConvAdapter()
    ok, msg = adapter.execute(
        item_id=1,
        robot_id="CONV1",
        command="CONV_ALLOW_MOVE",
        payload=json.dumps({"duration_sec": -1.0}).encode("utf-8"),
    )
    assert ok is False
    assert msg == "invalid_duration_sec"


def test_conv_adapter_requires_robot_id() -> None:
    from services.core.adapters.conv_adapter import ConvAdapter

    adapter = ConvAdapter()
    ok, msg = adapter.execute(item_id=1, robot_id="", command="CONV_ALLOW_MOVE", payload=b"{}")
    assert ok is False
    assert msg == "conv_robot_id_required"
