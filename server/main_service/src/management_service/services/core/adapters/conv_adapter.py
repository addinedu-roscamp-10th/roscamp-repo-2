"""컨베이어 재가동 어댑터 — ToPAWait task 의 CONV_ALLOW_MOVE step 진입점.

`ToPAWait` task 시퀀스:
    1. NOOP
    2. WAIT_SUBTASK_COMPLETED "tostrg" — AMR 이 `ToINSP` pose (컨베이어 출구 상차 대기)
       에 도착할 때까지 차단
    3. CONV_ALLOW_MOVE — 본 어댑터가 호출됨. 이 시점에서 `INSP_COMPLETED` 를
       EventBridge 에 publish 한다. PR #9 의 EventGateway servicer 가 EventBridge
       구독을 통해 Jetson `WatchEvents` 스트림으로 forward → Jetson `esp_bridge`
       가 `INSP_COMPLETED` 수신 시 ESP32 `start` 명령 전송 → 컨베이어 4 초 RUN
       → 주물이 출구에서 대기 중인 AMR 로 이송.

설계 의도 (PR #11 안전성 강화):
    - INSP_COMPLETED 를 AI 추론 직후 (ai_adapter) 가 publish 하면 AMR 도착 전에
      컨베이어가 가동되어 주물이 unloading 위치로 떨어지면서도 AMR 이 없어
      다음 공정으로 이송 불가. 본 어댑터로 publish 시점을 옮겨 AMR 도착이
      ToPAWait step 2 에서 보장된 다음에만 컨베이어가 가동되도록 한다.
    - publish 실패는 warning 로깅만. 어댑터 자체는 success 반환하여 task 흐름은
      유지 (운영 정책: 다음 cycle 진입 차단 vs 진입 허용은 추후 결정).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from services.contracts.enums import EventType
from services.contracts.models import Event
from services.contracts.protocols import IEventBridge

logger = logging.getLogger(__name__)

CONV_ACTION = "CONV_ALLOW_MOVE"


class ConvAdapter:
    """컨베이어 재가동 adapter — CONV_ALLOW_MOVE 수신 시 INSP_COMPLETED publish."""

    def __init__(self, event_bridge: IEventBridge | None = None) -> None:
        self._event_bridge = event_bridge

    def execute(
        self,
        item_id: int,
        robot_id: str,
        command: str,
        payload: bytes,
    ) -> tuple[bool, str]:
        if not robot_id:
            return (False, "conv_robot_id_required")
        if command != CONV_ACTION:
            return (False, f"unsupported_conv_command:{command}")

        try:
            payload_dict = json.loads(payload.decode("utf-8")) if payload else {}
        except json.JSONDecodeError:
            return (False, "invalid_json_payload")

        duration_sec: float | None = None
        raw_duration = payload_dict.get("duration_sec")
        if raw_duration is not None:
            try:
                duration_sec = float(raw_duration)
            except (TypeError, ValueError):
                return (False, "invalid_duration_sec")
            if duration_sec < 0:
                return (False, "invalid_duration_sec")

        self._publish_inspection_completed(
            item_id=item_id, robot_id=robot_id, duration_sec=duration_sec,
        )
        return (True, "conv_allow_move_accepted")

    def close(self) -> None:
        pass

    # ---------- internal ---------------------------------------------------
    def _publish_inspection_completed(
        self,
        *,
        item_id: int,
        robot_id: str,
        duration_sec: float | None,
    ) -> None:
        if self._event_bridge is None:
            logger.info(
                "ConvAdapter: event_bridge 미주입 — INSP_COMPLETED publish skip "
                "item_id=%s",
                item_id,
            )
            return
        try:
            self._event_bridge.publish(
                Event(
                    event_type=EventType.INSP_COMPLETED,
                    item_id=item_id if item_id else None,
                    res_id=robot_id,
                    payload={
                        "item_id": item_id,
                        "res_id": robot_id,
                        "duration_sec": duration_sec,
                        "source": "conv_adapter.CONV_ALLOW_MOVE",
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            )
            logger.info(
                "ConvAdapter: INSP_COMPLETED published item_id=%s robot_id=%s "
                "duration_sec=%s",
                item_id, robot_id, duration_sec,
            )
        except Exception as exc:  # noqa: BLE001 — publish 실패가 task 흐름을 막지 않도록 격리
            logger.warning(
                "ConvAdapter: INSP_COMPLETED publish 실패 item_id=%s exc=%s",
                item_id, exc,
            )
