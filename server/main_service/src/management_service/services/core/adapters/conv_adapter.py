from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from services.legacy.command_queue import ConveyorCmd, queue as command_queue

logger = logging.getLogger(__name__)

CONV_ACTION = "CONV_ALLOW_MOVE"
ESP_RUN_COMMAND = "RUN"  # ESP32 펌웨어: ST_STOPPED → ST_POST_RUN 4 s 트리거


class ConvAdapter:
    """컨베이어 재가동 adapter — CONV_ALLOW_MOVE 수신 시 'RUN' 명령 enqueue."""

    def __init__(self) -> None:
        pass

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

        self._enqueue_run_command(
            item_id=item_id, robot_id=robot_id, duration_sec=duration_sec,
        )
        return (True, "conv_allow_move_accepted")

    def close(self) -> None:
        pass

    # ---------- internal ---------------------------------------------------
    def _enqueue_run_command(
        self,
        *,
        item_id: int,
        robot_id: str,
        duration_sec: float | None,
    ) -> None:
        """ESP32 `RUN` 명령을 command_queue 에 적재.

        실제 전송은 hardware_rpc.WatchConveyorCommands → Jetson CommandSubscriber 가 담당.
        duration_sec 는 metadata 로 payload 에 포함 — ESP32 펌웨어가 직접 사용하지는 않고
        (자체 POST_RUN_MS=4000 timer 사용) 로그 / 관측 목적.
        """
        payload: dict[str, float | int | str] = {
            "source": "conv_adapter.CONV_ALLOW_MOVE",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        if duration_sec is not None:
            payload["duration_sec"] = duration_sec
        try:
            command_queue.enqueue(
                ConveyorCmd(
                    robot_id=robot_id,
                    command=ESP_RUN_COMMAND,
                    payload=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
                    item_id=item_id or 0,
                    issued_at_iso=payload["occurred_at"],  # type: ignore[arg-type]
                    issued_by="conv_adapter.CONV_ALLOW_MOVE",
                )
            )
            logger.info(
                "ConvAdapter: ConveyorCmd(RUN) enqueued item_id=%s robot_id=%s "
                "duration_sec=%s",
                item_id, robot_id, duration_sec,
            )
        except Exception as exc:  # noqa: BLE001 — enqueue 실패가 task 흐름을 막지 않도록 격리
            logger.warning(
                "ConvAdapter: ConveyorCmd(RUN) enqueue 실패 item_id=%s exc=%s",
                item_id, exc,
            )
