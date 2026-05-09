"""후처리 작업자 화면 (Post-Processing Worker) — 2026-04-26.

시나리오 (1.7.0 + 2026-05-08 책임 재배치):
  1. AMR 이 ToPP 로 도착하면 작업자가 "핸드오프 ACK" 버튼을 누른다 → handoff
  2. 작업자가 RFID 리더기로 주물을 스캔 →
     (a) Jetson esp_bridge → backend record_rfid_scan → rfid_scan_log INSERT +
         latest_rfid in-memory store 갱신
     (b) 본 페이지가 1 초 polling 으로 latest 가져와 _payload_edit 자동 채움
     (c) 작업자가 ② RFID 스캔 버튼 클릭 → lookup_item_by_rfid (read-only) →
         item_id / cur_stat / 후처리 옵션 표시 (상태 변경 X)
  3. 후처리 작업 완료 + RFID 부착 후 작업자가 "③ 후처리 완료" 버튼을 누르면
     3 초 카운트다운 후 컨베이어 모터 구동 + PP→ToINSP 전이 동시 수행
     (POST /api/management/conveyor/CONV-01/start?item_id=N →
       backend ESP32 dispatch (motor on) → _apply_pp_done_transition
       (PP→WAIT_INSP + ToINSP equip_task_txn PROC + equip_stat ON))
  4. 카메라 앞 TOF1 이 주물을 감지 → 펌웨어 motorOff + ST_STOPPED → Jetson 캡처 +
     UploadInspectionImage RPC → backend ToINSP→INSP 전이.

  ※ 시작 트리거 = 본 페이지 "후처리 완료" 버튼 (③) — 컨베이어 입구 센서 없음.
  ※ 정지 트리거 = TOF1 (카메라 앞) — 검사를 위해 cast 가 카메라 앞 도달 시 모터 정지.
  ※ TOF2 sensor 는 1.5.x 에서 hardware 제거 후 1.7.0 에서 코드까지 완전 제거됨.
  ※ 책임 재배치 (2026-05-08): RFID 스캔 = 조회 / 후처리 완료 = 전이 + 모터 구동.
    record_rfid_scan 은 추적·스토어만 담당.

본 페이지의 컨트롤 요소:
  ① 핸드오프 ACK 버튼  (시뮬, 실 GPIO33 푸시버튼 동등),
  ② RFID 스캔 버튼     (시뮬, 실 RC522 NDEF Text 동등),
  ③ 후처리 완료 버튼   (실 컨베이어 모터 구동 + backend 상태 전이),
  ④ TOF1 디스플레이    (비클릭, ON=녹색 / OFF=회색 — 카메라 앞 정지 센서 상태 표시).

레이아웃:
  ┌──── 상단 컨트롤 ───────────────────────────────────────────────────┐
  │  RFID payload [ ____________ ]  [① 핸드오프 ACK] [② RFID 스캔]      │
  │  [③ 후처리 완료 (3 초 후 컨베이어 구동)]  [카운트다운] [취소]        │
  │  ●  ④ TOF1 (카메라 앞 정지 센서)  ← 디스플레이 (클릭 안됨)            │
  │  상태: ...                                                          │
  └──────────────────────────────────────────────────────────────────────┘
  ┌──── 본문 ───────────────────────────────────────────────────────────┐
  │  item 정보 (item_id / cur_stat / equip_task_type / cur_res / ord_id) │
  ├─────────────────────────────────────────────────────────────────────┤
  │  후처리 옵션 표 (pp_nm / extra_cost / 진행 상태)                    │
  └─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.api_client import ApiClient


class PpWorkerPage(QWidget):
    """후처리 작업자 시뮬 화면 — 핸드오프 / RFID / TOF1 / TOF2."""

    refresh_requested = pyqtSignal()

    # 후처리 완료 → 컨베이어 구동 카운트다운 (2026-05-08).
    PP_DONE_COUNTDOWN_S: int = 3
    PP_DONE_RES_ID: str = "CONV-01"  # ESP32 식별자 — Mgmt ExecuteCommand robot_id
    # 1.7.0: sensor state 는 publisher 가 ESP_BRIDGE_CONVEYOR_RES_ID="CONV1" (DB schema)
    # 로 push 하므로 PyQt poll 도 동일 키 사용.
    SENSOR_DB_RES_ID: str = "CONV1"
    # TOF1 indicator polling 주기 (1.7.0, 2026-05-08).
    # Jetson publisher 가 status snapshot edge 에 push (~335ms 단위) → backend 즉시 반영.
    # PyQt 가 250ms polling 시 worst-case 지연 ≈ 335 + 250 ≈ 585ms (실용적 실시간).
    TOF1_POLL_INTERVAL_MS: int = 250
    # RFID payload 자동 채움 polling 주기 (책임 재배치, 2026-05-08).
    # 실 RC522 가 ESP32 → Jetson → backend record_rfid_scan 까지 도달하면
    # /api/rfid/<reader_id>/latest in-memory 갱신 → 본 timer 가 다음 tick 에 가져와
    # _payload_edit 자동 채움. RFID 빈도 낮으므로 1 초로 충분.
    RFID_POLL_INTERVAL_MS: int = 1000
    RFID_READER_ID: str = "RFID-CONV-01"  # Jetson env ESP_BRIDGE_RFID_READER_ID 와 일치

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._api = api
        self._current_item_id: int | None = None
        self._current_payload: str = ""
        # 카운트다운 상태 — _build_ui 가 위젯 만들기 전에 초기화 필요.
        self._pp_done_remaining: int = 0
        self._pp_done_timer = QTimer(self)
        self._pp_done_timer.setInterval(1000)  # 1 초 tick
        self._pp_done_timer.timeout.connect(self._pp_done_tick)
        # TOF1 indicator polling timer (1.7.0).
        self._tof1_poll_timer = QTimer(self)
        self._tof1_poll_timer.setInterval(self.TOF1_POLL_INTERVAL_MS)
        self._tof1_poll_timer.timeout.connect(self._poll_tof1_state)
        # RFID payload 자동 채움 polling timer (책임 재배치, 2026-05-08 / 2026-05-09 fix).
        # 시작 시점으로 초기화 → 시작 이전의 stale 스캔(어제 데이터)은 자동 입력 무시.
        # 신규 스캔만 _payload_edit 자동 채움.
        import time as _time

        self._last_auto_payload_scanned_at: float = _time.time()
        self._rfid_poll_timer = QTimer(self)
        self._rfid_poll_timer.setInterval(self.RFID_POLL_INTERVAL_MS)
        self._rfid_poll_timer.timeout.connect(self._poll_latest_rfid)
        self._build_ui()
        # 페이지 visible 여부와 무관하게 indicator 가 항상 최신 — page 생성과 동시에 시작.
        self._tof1_poll_timer.start()
        self._rfid_poll_timer.start()

        # 매뉴얼 캡처/오프라인 데모 — RFID 스캔 결과를 즉시 시뮬레이션
        import os as _os

        if _os.environ.get("CASTING_DATA_MODE") == "mock_only":
            try:
                demo = self._api.lookup_item_by_rfid("order_101_item_20260501_4")
                if isinstance(demo, dict):
                    self._render_item(demo.get("item") or {})
                    self._render_options(demo.get("pp_options") or [])
                    item = demo.get("item") or {}
                    if item.get("item_id"):
                        self._current_item_id = int(item["item_id"])
                    if hasattr(self, "_payload_edit"):
                        self._payload_edit.setText("order_101_item_20260501_4")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # UI build
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # ---- 작업자 로그인 (작업 3) ----
        login_box = QGroupBox("작업자 로그인")
        login_grid = QGridLayout(login_box)
        login_grid.addWidget(QLabel("이메일:"), 0, 0)
        self._email_edit = QLineEdit()
        self._email_edit.setPlaceholderText("operator@example.com")
        self._email_edit.setMinimumWidth(280)
        login_grid.addWidget(self._email_edit, 0, 1)

        self._btn_login = QPushButton("로그인")
        self._btn_login.clicked.connect(self._on_login)
        login_grid.addWidget(self._btn_login, 0, 2)

        self._btn_logout = QPushButton("로그아웃")
        self._btn_logout.clicked.connect(self._on_logout)
        login_grid.addWidget(self._btn_logout, 0, 3)

        self._operator_label = QLabel("현재: 비로그인")
        self._operator_label.setProperty("tone", "muted")
        login_grid.addWidget(self._operator_label, 0, 4)
        login_grid.setColumnStretch(4, 1)

        root.addWidget(login_box)

        # ---- 상단 컨트롤 ----
        ctrl_box = QGroupBox("후처리 작업자 컨트롤")
        grid = QGridLayout(ctrl_box)
        grid.setSpacing(8)

        grid.addWidget(QLabel("RFID payload:"), 0, 0)
        self._payload_edit = QLineEdit()
        # 2026-05-09: 첫 시작 시 자동 focus 차단 — RFID 자동 채움 polling 의 setText 가
        # 즉시 paint 되도록. 사용자가 직접 입력 시는 마우스 클릭으로 focus 획득.
        self._payload_edit.setFocusPolicy(Qt.ClickFocus)
        self._payload_edit.setPlaceholderText("order_17_item_20260417_7")
        self._payload_edit.setMinimumWidth(320)
        grid.addWidget(self._payload_edit, 0, 1)

        self._btn_handoff = QPushButton("① 핸드오프 ACK (버튼)")
        self._btn_handoff.clicked.connect(self._on_handoff_ack)
        grid.addWidget(self._btn_handoff, 0, 2)

        self._btn_scan = QPushButton("② RFID 스캔")
        self._btn_scan.clicked.connect(self._on_rfid_scan)
        grid.addWidget(self._btn_scan, 0, 3)

        # ③ 후처리 완료 — 3 초 카운트다운 후 실 컨베이어 모터 구동.
        # 종전(2026-04~05) "TOF1 입구 트리거" 폐기 후 새 시작 신호.
        self._btn_pp_done = QPushButton("③ 후처리 완료 — 컨베이어 3 초 후 구동")
        self._btn_pp_done.setProperty("tone", "primary")
        self._btn_pp_done.setToolTip(
            "후처리 작업이 끝났음을 알리고 컨베이어 모터를 구동합니다.\n"
            "버튼 클릭 → 3 초 카운트다운 → POST /api/management/conveyor/CONV-01/start "
            "→ ESP32 firmware motor ON."
        )
        self._btn_pp_done.clicked.connect(self._on_pp_done)
        grid.addWidget(self._btn_pp_done, 1, 0, 1, 2)

        self._pp_done_countdown_label = QLabel("")
        self._pp_done_countdown_label.setAlignment(Qt.AlignCenter)
        self._pp_done_countdown_label.setProperty("tone", "muted")
        grid.addWidget(self._pp_done_countdown_label, 1, 2)

        self._btn_pp_done_cancel = QPushButton("취소")
        self._btn_pp_done_cancel.setToolTip("3 초 카운트다운 중에만 활성화")
        self._btn_pp_done_cancel.clicked.connect(self._on_pp_done_cancel)
        self._btn_pp_done_cancel.setEnabled(False)
        grid.addWidget(self._btn_pp_done_cancel, 1, 3)

        # ④ TOF1 감지 — 비클릭형 ON/OFF 디스플레이 (1.7.0).
        # 카메라 앞 정지 센서. ON (녹색) = cast 가 카메라 앞 도달 / OFF (회색) = 미감지.
        # 데이터 source 는 후속 (publisher → backend stream). 현재는 default OFF (회색).
        self._tof1_indicator_dot = QLabel("●")
        self._tof1_indicator_dot.setObjectName("tof1IndicatorDot")
        self._tof1_indicator_dot.setAlignment(Qt.AlignCenter)
        self._tof1_indicator_dot.setFixedWidth(32)
        self._tof1_indicator_label = QLabel("④ TOF1 (카메라 앞 정지 센서)")
        self.set_tof1_state(False)  # default OFF (회색)
        grid.addWidget(self._tof1_indicator_dot, 2, 2)
        grid.addWidget(self._tof1_indicator_label, 2, 3)

        self._status_label = QLabel("")
        self._status_label.setProperty("tone", "muted")
        self._status_label.setWordWrap(True)
        grid.addWidget(self._status_label, 3, 0, 1, 4)

        root.addWidget(ctrl_box)

        # ---- item 정보 ----
        item_box = QGroupBox("선택 item 정보")
        item_grid = QGridLayout(item_box)
        item_grid.setSpacing(6)
        self._item_labels: dict[str, QLabel] = {}
        for row, key, label in [
            (0, "item_id", "item_id"),
            (0, "ord_id", "ord_id"),
            (1, "cur_stat", "현재 공정 (cur_stat)"),
            (1, "equip_task_type", "equip_task_type"),
            (2, "cur_res", "점유 자원 (cur_res)"),
            (2, "is_defective", "불량 여부"),
        ]:
            cell_label = QLabel(f"{label}:")
            cell_label.setObjectName("itemFieldLabel")
            cell_value = QLabel("-")
            cell_value.setProperty("tone", "primary")
            self._item_labels[key] = cell_value
            col = 0 if label.startswith(("item_id", "현재", "점유")) else 2
            item_grid.addWidget(cell_label, row, col)
            item_grid.addWidget(cell_value, row, col + 1)

        root.addWidget(item_box)

        # ---- 후처리 옵션 표 ----
        opts_box = QGroupBox("필요 후처리 옵션 (정의 + 진행 현황)")
        opts_v = QVBoxLayout(opts_box)
        self._opts_table = QTableWidget(0, 5)
        self._opts_table.setHorizontalHeaderLabels(
            ["pp_id", "pp_nm", "extra_cost", "txn_stat", "txn_id"]
        )
        self._opts_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._opts_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._opts_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._opts_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._opts_table.setVisible(False)
        opts_v.addWidget(self._opts_table)

        # 빈 상태 안내 — RFID 스캔 전에는 항상 보임
        self._opts_empty_label = QLabel("RFID 스캔 후 후처리 옵션이 여기에 표시됩니다.")
        self._opts_empty_label.setAlignment(Qt.AlignCenter)
        self._opts_empty_label.setProperty("tone", "muted")
        opts_v.addWidget(self._opts_empty_label)

        root.addWidget(opts_box, 1)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _set_status(self, msg: str, ok: bool = True) -> None:
        # tone 변경 시 QSS 재계산을 위해 unpolish/polish 트리거
        self._status_label.setProperty("tone", "ok" if ok else "danger")
        style = self._status_label.style()
        if style is not None:
            style.unpolish(self._status_label)
            style.polish(self._status_label)
        self._status_label.setText(msg)

    def _payload(self) -> str:
        return (self._payload_edit.text() or "").strip()

    # ---- 로그인 ----
    def _on_login(self) -> None:
        email = (self._email_edit.text() or "").strip()
        if not email:
            self._set_status("이메일 입력 필요", ok=False)
            return
        try:
            r = self._api.auth_lookup(email)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "로그인 실패", str(exc))
            self._set_status(f"로그인 실패: {exc}", ok=False)
            self._operator_label.setText("현재: 비로그인")
            return
        if not r:
            self._set_status("응답 없음", ok=False)
            return
        self._operator_label.setText(f"현재: {self._api.current_operator_label()}")
        self._set_status(
            f"로그인 OK — user_id={r.get('user_id')} 이후 후처리 작업은 자동으로 operator_id 기록"
        )

    def _on_logout(self) -> None:
        self._api.__init_operator__()
        self._api._operator = None  # noqa: SLF001
        self._operator_label.setText("현재: 비로그인")
        self._set_status("로그아웃 완료")

    def _on_handoff_ack(self) -> None:
        try:
            r = self._api.post_handoff_ack()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "핸드오프 실패", str(exc))
            self._set_status(f"핸드오프 실패: {exc}", ok=False)
            return
        if not r:
            self._set_status("핸드오프 응답 없음 (서버 또는 mock_only 모드)", ok=False)
            return
        if r.get("released"):
            ord_id = r.get("ord_id")
            item_id = r.get("item_id")
            self._set_status(
                f"① 핸드오프 OK — AMR={r.get('amr_id')} item_id={item_id} "
                f"ord_id={ord_id} pp_task QUE={r.get('pp_task_txn_ids')} → "
                f"item.cur_stat={r.get('item_cur_stat')}"
            )
            # payload 자동 채움
            if item_id and ord_id:
                self._payload_edit.setText(f"order_{ord_id}_item_20260417_{item_id}")
            self._current_item_id = item_id
            # 2026-05-08 EventGateway channel — PyQt 핸드오프 ACK 버튼 publish.
            # 하드웨어 GPIO33 버튼과 동일한 의미 (HANDOFF_ACK). 실패 silent.
            try:
                from app.clients.event_gateway import publish_event as _publish_eg

                _publish_eg(
                    event_type="HANDOFF_ACK",
                    resource_id="CONV1",
                    payload={
                        "zone": "postprocessing",
                        "source_device": "pyqt-pp-worker",
                        "button": "ui-handoff",
                        "amr_id": r.get("amr_id") or "",
                        "item_id": int(item_id) if item_id else 0,
                        "ord_id": int(ord_id) if ord_id else 0,
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        else:
            self._set_status(
                f"① 핸드오프 orphan — 대기 AMR 없음 (reason={r.get('reason')})",
                ok=False,
            )

    def _on_rfid_scan(self) -> None:
        """② RFID 스캔 버튼 — payload 로 item + 후처리 옵션 정보 **조회만** (상태 변경 X).

        2026-05-08 책임 재배치: PP→ToINSP 전이는 ③ 후처리 완료 버튼이 담당.
        본 핸들러는 ``lookup_item_by_rfid`` (read-only) 로 화면 정보 표시만 수행.
        """
        payload = self._payload()
        if not payload:
            self._set_status("RFID payload 입력 필요 (예: order_17_item_20260417_7)", ok=False)
            return
        try:
            r = self._api.lookup_item_by_rfid(payload)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "RFID 스캔 실패", str(exc))
            self._set_status(f"② RFID 조회 실패: {exc}", ok=False)
            return
        if not r:
            self._set_status(
                f"② RFID 매칭 실패 — payload={payload} (item not found)", ok=False,
            )
            return
        self._current_payload = payload
        item = r.get("item") or {}
        if not item:
            self._set_status(f"② RFID 응답 형식 오류 — payload={payload}", ok=False)
            return
        pp_options = r.get("pp_options") or []
        self._render_item(item)
        self._render_options(pp_options)
        self._current_item_id = item.get("item_id")
        self._set_status(
            f"② RFID 조회 OK — item_id={item.get('item_id')} "
            f"cur_stat={item.get('cur_stat')} 옵션={len(pp_options)}건 "
            "(상태 변경은 ③ 후처리 완료 시점)"
        )

    # ---- 후처리 완료 → 3 초 후 컨베이어 구동 ----
    def _on_pp_done(self) -> None:
        """후처리 완료 버튼 핸들러 — 3 초 카운트다운 시작."""
        if self._pp_done_timer.isActive():
            # 이미 카운트다운 중 — 무시 (UI 가 비활성이라 도달하지 않지만 방어).
            return
        self._pp_done_remaining = self.PP_DONE_COUNTDOWN_S
        self._btn_pp_done.setEnabled(False)
        self._btn_pp_done_cancel.setEnabled(True)
        self._update_pp_done_label()
        self._set_status(
            f"③ 후처리 완료 입력 — {self.PP_DONE_COUNTDOWN_S} 초 후 컨베이어 구동 (취소 가능)"
        )
        self._pp_done_timer.start()

    def _pp_done_tick(self) -> None:
        """매 1 초 카운트다운. 0 도달 시 dispatch."""
        self._pp_done_remaining -= 1
        if self._pp_done_remaining > 0:
            self._update_pp_done_label()
            return
        self._pp_done_timer.stop()
        self._update_pp_done_label()
        self._pp_done_dispatch()

    def _on_pp_done_cancel(self) -> None:
        """카운트다운 중 취소."""
        if not self._pp_done_timer.isActive():
            return
        self._pp_done_timer.stop()
        self._pp_done_remaining = 0
        self._reset_pp_done_ui()
        self._set_status("③ 후처리 완료 — 카운트다운 취소", ok=True)

    def _pp_done_dispatch(self) -> None:
        """3 초 경과 시점에 실 컨베이어 start 명령 dispatch."""
        # 2026-05-08 EventGateway channel — 사용자 후처리 완료 의도 publish.
        # 카운트다운 후 dispatch 시점에 발행 (취소 시 미발행).
        try:
            from app.clients.event_gateway import publish_event as _publish_eg

            _publish_eg(
                event_type="PP_DONE_REQUESTED",
                resource_id=self.SENSOR_DB_RES_ID,  # "CONV1"
                payload={
                    "source_device": "pyqt-pp-worker",
                    "button": "ui-pp-done",
                    "item_id": int(self._current_item_id) if self._current_item_id else 0,
                    "rfid_payload": self._current_payload or "",
                },
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            r = self._api.post_conveyor_start(
                res_id=self.PP_DONE_RES_ID,
                item_id=self._current_item_id or 0,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "컨베이어 구동 실패", str(exc))
            self._set_status(f"③ 후처리 완료 → 컨베이어 구동 실패: {exc}", ok=False)
            self._reset_pp_done_ui()
            return
        self._reset_pp_done_ui()
        if not r:
            # mock_only / 응답 없음 — 사용자가 의도한 dev 환경일 수 있으므로 ok 표시.
            self._set_status(
                "③ 후처리 완료 → 응답 없음 (mock_only / 백엔드 미가동) — 시뮬 모드로 가정",
                ok=True,
            )
            return
        # 2026-05-08 책임 재배치: backend conveyor/start 가 ESP32 dispatch 후
        # _apply_pp_done_transition 으로 PP→ToINSP 전이 + equip_stat ON 동시 처리.
        # 응답의 transition dict 를 status 메시지에 함께 표시.
        transition = r.get("transition") or {}
        trans_msg = ""
        if transition:
            if transition.get("pp_to_toinsp_applied"):
                trans_msg = (
                    f" / PP→{transition.get('item_cur_stat_after')} "
                    f"equip_txn={transition.get('equip_task_txn_id')}"
                )
            elif transition.get("pp_to_toinsp_error"):
                trans_msg = f" / 전이 실패={transition.get('pp_to_toinsp_error')}"
            elif transition.get("item_cur_stat_before"):
                trans_msg = (
                    f" / 전이 skip (cur_stat={transition.get('item_cur_stat_before')!r})"
                )
        if r.get("accepted"):
            self._set_status(
                f"③ 후처리 완료 → 컨베이어 구동 OK — res={r.get('res_id')} "
                f"action={r.get('action')}{trans_msg}"
            )
        else:
            self._set_status(
                f"③ 후처리 완료 → 컨베이어 거부됨 — reason={r.get('reason') or '<empty>'}{trans_msg}",
                ok=False,
            )

    def _update_pp_done_label(self) -> None:
        """카운트다운 라벨 텍스트 갱신 (남은 초수 → '3 초 후 구동' 등)."""
        if self._pp_done_remaining > 0:
            self._pp_done_countdown_label.setProperty("tone", "warning")
            self._pp_done_countdown_label.setText(
                f"{self._pp_done_remaining} 초 후 컨베이어 구동…"
            )
        else:
            self._pp_done_countdown_label.setProperty("tone", "muted")
            self._pp_done_countdown_label.setText("")
        # tone property 변경 시 QSS 재계산
        style = self._pp_done_countdown_label.style()
        if style is not None:
            style.unpolish(self._pp_done_countdown_label)
            style.polish(self._pp_done_countdown_label)

    def _reset_pp_done_ui(self) -> None:
        """카운트다운 종료 후 버튼/라벨 평시 상태로 복귀."""
        self._pp_done_remaining = 0
        self._update_pp_done_label()
        self._btn_pp_done.setEnabled(True)
        self._btn_pp_done_cancel.setEnabled(False)

    # ---- TOF1 indicator polling (1.7.0~) ----
    def _poll_tof1_state(self) -> None:
        """250ms 마다 backend in-memory sensor state 조회 → indicator 갱신.

        Jetson publisher 가 status snapshot edge 에서 push 하므로 backend 응답은
        가장 최신 상태. publisher 가 ESP_BRIDGE_CONVEYOR_RES_ID="CONV1" (DB schema)
        로 push 하므로 SENSOR_DB_RES_ID="CONV1" 로 polling.
        응답 실패 (mock_only / 백엔드 미가동) 시 조용히 skip.
        """
        try:
            r = self._api.get_sensor_state(res_id=self.SENSOR_DB_RES_ID, sensor_id="tof1")
        except Exception:  # noqa: BLE001 — 네트워크 실패는 indicator 갱신만 skip, page 동작 무영향
            return
        if not isinstance(r, dict):
            return
        on = bool(r.get("on", False))
        # 현재 setter property 와 다를 때만 stylesheet 재적용 (불필요한 paint 방어)
        if self._tof1_indicator_dot.property("on") != on:
            self.set_tof1_state(on)

    # ---- RFID payload 자동 채움 polling (책임 재배치, 2026-05-08~) ----
    def _poll_latest_rfid(self) -> None:
        """1 초마다 backend `/api/rfid/<reader_id>/latest` 조회 → _payload_edit 자동 채움.

        Source: state_manager.record_rfid_scan 가 successful scan 시 in-memory store 갱신.
        Jetson esp_bridge → backend gRPC → record_rfid_scan → latest store → 본 timer 가 가져옴.

        보호 가드:
            - `_payload_edit.hasFocus()` 시: 사용자 수동 편집 중 → 자동 덮어쓰기 skip
            - `scanned_at == self._last_auto_payload_scanned_at`: 동일 스캔 재진입 → skip
            - 응답 실패 (mock_only / 백엔드 미가동) 시: 조용히 skip
        """
        try:
            r = self._api.get_latest_rfid_for_reader(self.RFID_READER_ID)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(r, dict):
            return
        payload = r.get("payload")
        scanned_at = r.get("scanned_at")
        if not payload or scanned_at is None:
            return
        # 시작 시각 이후 새 스캔만 자동 입력 (stale 무시 + 동일 스캔 중복 방지).
        if scanned_at <= self._last_auto_payload_scanned_at:
            return
        self._last_auto_payload_scanned_at = scanned_at
        self._payload_edit.setText(str(payload))
        # 2026-05-09 fix: 사용자가 다른 위젯에 focus 두고 있을 때도 즉시 화면 갱신.
        # repaint() 는 동기 paint 호출 — Qt event loop 의 paint 큐 대기 회피.
        self._payload_edit.repaint()
        self._set_status(
            f"RFID 자동 입력: {payload} — ② RFID 스캔 버튼을 눌러 정보 조회",
            ok=True,
        )

    # ---- TOF1 indicator setter (외부 worker 가 호출, 1.7.0~) ----
    def set_tof1_state(self, on: bool) -> None:
        """TOF1 (카메라 앞 정지 센서) ON/OFF 시각 갱신.

        ON  → 녹색 (#4caf50): cast 가 카메라 앞 도달
        OFF → 회색 (#757575): 미감지

        데이터 source 는 후속 작업으로 publisher → backend → PyQt 경로 추가 예정.
        현재는 외부 worker 가 명시적으로 호출하기 전까지 default OFF (회색) 유지.
        """
        color = "#4caf50" if on else "#757575"
        self._tof1_indicator_dot.setStyleSheet(
            f"QLabel#tof1IndicatorDot {{ color: {color}; font-size: 22px; font-weight: bold; }}"
        )
        self._tof1_indicator_dot.setProperty("on", on)

    def _refresh_lookup(self, payload: str) -> None:
        try:
            r = self._api.lookup_item_by_rfid(payload)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"lookup 실패: {exc}", ok=False)
            return
        if not r:
            return
        self._render_item(r.get("item") or {})
        self._render_options(r.get("pp_options") or [])

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def _render_item(self, item: dict[str, Any]) -> None:
        for key, lbl in self._item_labels.items():
            v = item.get(key)
            lbl.setText("-" if v is None else str(v))

    def _render_options(self, options: list[dict[str, Any]]) -> None:
        self._opts_table.setRowCount(len(options))
        # 빈 상태 레이블: 옵션이 없을 때만 표시
        self._opts_empty_label.setVisible(len(options) == 0)
        self._opts_table.setVisible(len(options) > 0)
        for r, opt in enumerate(options):
            cells = [
                str(opt.get("pp_id", "")),
                str(opt.get("pp_nm", "")),
                "-" if opt.get("extra_cost") is None else f"{int(opt['extra_cost']):,}",
                str(opt.get("txn_stat") or "-"),
                str(opt.get("txn_id") or "-"),
            ]
            for c, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setTextAlignment(Qt.AlignCenter)
                if c == 3:  # txn_stat
                    if opt.get("txn_stat") == "SUCC":
                        cell.setForeground(Qt.darkGreen)
                    elif opt.get("txn_stat") == "FAIL":
                        cell.setForeground(Qt.red)
                self._opts_table.setItem(r, c, cell)
