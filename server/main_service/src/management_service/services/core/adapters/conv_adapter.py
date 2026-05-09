from __future__ import annotations

import json

CONV_ACTION = "CONV_ALLOW_MOVE"


class ConvAdapter:
    """컨베이어 재가동 adapter"""

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

        duration_sec = payload_dict.get("duration_sec")
        if duration_sec is not None:
            try:
                duration_sec = float(duration_sec)
            except (TypeError, ValueError):
                return (False, "invalid_duration_sec")
            if duration_sec < 0:
                return (False, "invalid_duration_sec")

        # TODO: 컨베이어 재가동 구현 위치
        # 실제 컨베이어 진행 명령을 보내기.
        # 성공하면 (True, "..."), 실패하면 (False, "...") 반환
        return (True, "conv_allow_move_accepted")

    def close(self) -> None:
        pass
