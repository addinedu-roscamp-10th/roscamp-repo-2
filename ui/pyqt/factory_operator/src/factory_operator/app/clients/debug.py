"""Debug 도메인 mixin — RFID / Conveyor TOF1 시뮬 (개발 모드, 2026-05-08 1.7.0).

후처리 작업자 화면의 가상 시뮬 endpoint. 실 HW (ESP32 RC522 → Mgmt gRPC) 미연동 환경에서
PyQt 만으로 e2e 테스트. TOF2 관련 sim 메서드는 1.7.0 에서 제거됨 (TOF2 sensor hardware
+ firmware 코드 모두 제거 완료).

Backend `/api/debug/*` endpoint 는 APP_ENV=development 일 때만 등록됨.
"""

from __future__ import annotations

from typing import Any

from app import mock_data


class DebugMixin:
    """개발 모드 가상 시뮬 endpoints (RFID + TOF1/TOF2)."""

    def lookup_item_by_rfid(self, payload: str) -> dict[str, Any] | None:
        """RFID payload(`order_<ord>_item_<YYYYMMDD>_<seq>`)로 item + 옵션 조회.

        응답: {item: {...}, pp_options: [{pp_nm, txn_stat, ...}, ...]}
        """
        from urllib.parse import quote

        return self._get(
            f"/api/debug/items/by-rfid?payload={quote(payload)}",
            mock_value=mock_data.RFID_LOOKUP_RESULT,
        )

    def post_sim_rfid_scan(
        self, raw_payload: str, reader_id: str = "PYQT-WORKER", zone: str = "postprocessing"
    ) -> dict[str, Any] | None:
        """가상 RFID 스캔 — rfid_scan_log INSERT + item 매칭."""
        return self._post(
            "/api/debug/sim/rfid-scan",
            {
                "reader_id": reader_id,
                "zone": zone,
                "raw_payload": raw_payload,
            },
        )

    def get_latest_rfid_for_reader(
        self, reader_id: str = "RFID-CONV-01"
    ) -> dict[str, Any] | None:
        """가장 최근 RFID 스캔 payload 조회 — PyQt _payload_edit 자동 채움.

        backend in-memory store (`/api/rfid/<reader_id>/latest`) 에서 가져옴.
        record_rfid_scan 이 successful scan 시 갱신. 미수신 시 ``payload=None``.

        2026-05-08: 책임 재배치 — RFID 스캔 책임은 후처리 완료 버튼으로 이관.
        본 endpoint 는 자동 입력만 위한 read-only.
        """
        from urllib.parse import quote

        return self._get(f"/api/rfid/{quote(reader_id)}/latest")

    def post_conveyor_start(
        self, res_id: str = "CONV-01", item_id: int = 0
    ) -> dict[str, Any] | None:
        """실 ESP32 컨베이어 모터 ON — `start` 명령 dispatch.

        2026-05-08: PP 작업자 페이지 "후처리 완료" 버튼 → 3 초 카운트다운 후 호출.
        Management ExecuteCommand RPC → JetsonRelayAdapter → ConveyorCommandQueue →
        Jetson CommandSubscriber → EspBridge.send_command("start") → ESP32 펌웨어
        handleCommand("start") → motorOn() + ST_RUNNING (즉시 진입).

        Sim endpoints (post_sim_conveyor_tof1/tof2) 와 달리 실 모터를 구동하므로
        디버그 환경 (`APP_ENV=development`) 외에서도 동작.

        반환 (mock_only 모드에서는 None):
          {"res_id": "CONV-01", "action": "start", "command": "start",
           "accepted": bool, "reason": str}
        """
        return self._post(
            f"/api/management/conveyor/{res_id}/start?item_id={item_id}",
            {},
        )
