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
    QAction,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
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
        # 2026-05-10: HTTP polling 제거 — EventGateway WatchEvents subscribe 로 통일.
        #   RFID_SCANNED       → _payload_edit 자동 채움
        #   TOF1_ENTRY         → indicator 색 갱신
        #   ITEM_LOOKUP_RESULT → ② 버튼 응답 (item + pp_options)
        # 시작 시점으로 초기화 → 그 이전의 stale 이벤트 무시.
        import time as _time

        self._last_auto_payload_scanned_at: float = _time.time()
        self._eg_watcher: object | None = None  # WatchEventsThread, 후 setup
        self._build_ui()
        self._setup_event_gateway_subscribe()

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
        login_box.setObjectName("ppLoginBox")
        login_grid = QGridLayout(login_box)
        login_grid.addWidget(QLabel("이메일:"), 0, 0)
        self._email_edit = QLineEdit()
        self._email_edit.setPlaceholderText("operator@example.com")
        self._email_edit.setMinimumWidth(280)
        login_grid.addWidget(self._email_edit, 0, 1)

        self._btn_fetch_operators = QPushButton("불러오기 ▼")
        self._btn_fetch_operators.setToolTip("DB에서 작업자 목록을 가져와 선택합니다")
        self._btn_fetch_operators.clicked.connect(self._on_fetch_operators)
        login_grid.addWidget(self._btn_fetch_operators, 0, 2)

        self._btn_login = QPushButton("로그인")
        self._btn_login.clicked.connect(self._on_login)
        login_grid.addWidget(self._btn_login, 0, 3)

        self._btn_logout = QPushButton("로그아웃")
        self._btn_logout.clicked.connect(self._on_logout)
        login_grid.addWidget(self._btn_logout, 0, 4)

        self._operator_label = QLabel("현재: 비로그인")
        self._operator_label.setProperty("tone", "muted")
        login_grid.addWidget(self._operator_label, 0, 5)
        login_grid.setColumnStretch(5, 1)

        root.addWidget(login_box)

        # ---- 상단 컨트롤 ----
        ctrl_box = QGroupBox("후처리 작업자 컨트롤")
        ctrl_box.setObjectName("ppCtrlBox")
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

        self._btn_handoff = QPushButton("① 하차완료")
        self._btn_handoff.clicked.connect(self._on_handoff_ack)
        grid.addWidget(self._btn_handoff, 0, 2)

        self._btn_scan = QPushButton("② 후처리 검색")
        self._btn_scan.clicked.connect(self._on_rfid_scan)
        grid.addWidget(self._btn_scan, 0, 3)

        # ③ 후처리 완료 — 3 초 카운트다운 후 실 컨베이어 모터 구동.
        # 종전(2026-04~05) "TOF1 입구 트리거" 폐기 후 새 시작 신호.
        self._btn_pp_done = QPushButton("③ 후처리 완료")
        self._btn_pp_done.setProperty("tone", "primary")
        self._btn_pp_done.setToolTip(
            "후처리 작업이 끝났음을 알리고 컨베이어 모터를 구동합니다.\n"
            "버튼 클릭 → 3 초 카운트다운 → POST /api/management/conveyor/CONV-01/start "
            "→ ESP32 firmware motor ON."
        )
        self._btn_pp_done.clicked.connect(self._on_pp_done)
        grid.addWidget(self._btn_pp_done, 1, 2)

        self._btn_pp_done_cancel = QPushButton("취소")
        self._btn_pp_done_cancel.setToolTip("3 초 카운트다운 중에만 활성화")
        self._btn_pp_done_cancel.clicked.connect(self._on_pp_done_cancel)
        self._btn_pp_done_cancel.setEnabled(False)
        grid.addWidget(self._btn_pp_done_cancel, 1, 3)

        self._pp_done_countdown_label = QLabel("")
        self._pp_done_countdown_label.setAlignment(Qt.AlignCenter)
        self._pp_done_countdown_label.setProperty("tone", "muted")
        grid.addWidget(self._pp_done_countdown_label, 2, 2, 1, 2)

        self._status_label = QLabel("")
        self._status_label.setProperty("tone", "muted")
        self._status_label.setWordWrap(True)
        grid.addWidget(self._status_label, 3, 0, 1, 4)

        root.addWidget(ctrl_box)

        # ---- item 정보 ----
        item_box = QGroupBox("선택 제품 정보")
        item_box.setObjectName("ppItemBox")
        item_grid = QGridLayout(item_box)
        item_grid.setSpacing(6)
        self._item_labels: dict[str, QLabel] = {}
        # (row, key, label, col_left) — col_left=True 이면 grid 왼쪽 0열, False 이면 2열.
        for row, key, label, col_left in [
            (0, "item_id", "제품 ID", True),
            (0, "ord_id", "주문 ID", False),
            (1, "cur_stat", "현재 단계", True),
            (1, "equip_task_type", "설비 작업 유형", False),
            (2, "cur_res", "점유 자원", True),
            (2, "is_defective", "불량 여부", False),
        ]:
            cell_label = QLabel(f"{label}:")
            cell_label.setObjectName("itemFieldLabel")
            cell_value = QLabel("-")
            cell_value.setProperty("tone", "primary")
            self._item_labels[key] = cell_value
            col = 0 if col_left else 2
            item_grid.addWidget(cell_label, row, col)
            item_grid.addWidget(cell_value, row, col + 1)

        root.addWidget(item_box)

        # ---- 후처리 옵션 표 ----
        opts_box = QGroupBox("필요 후처리 옵션 (정의 + 진행 현황)")
        opts_box.setObjectName("ppOptsBox")
        opts_v = QVBoxLayout(opts_box)
        self._opts_table = QTableWidget(0, 5)
        self._opts_table.setHorizontalHeaderLabels(
            ["후처리 코드", "후처리명", "추가 비용", "진행 상태", "작업 ID"]
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

    # ---- 작업자 목록 불러오기 ----
    def _on_fetch_operators(self) -> None:
        """gRPC ListOperators 로 DB 작업자 목록을 가져와 드롭다운 메뉴로 표시."""
        try:
            from app.management_client import ManagementClient
            client = ManagementClient()
            operators = client.list_operators(role_filter="operator")
            client.close()
        except Exception as exc:
            QMessageBox.warning(self, "작업자 목록 오류", f"gRPC 조회 실패:\n{exc}")
            return

        if not operators:
            QMessageBox.information(self, "작업자 목록", "등록된 작업자가 없습니다.")
            return

        menu = QMenu(self)
        for op in operators:
            label = f"{op['user_nm']}  ({op['email']})"
            action = QAction(label, self)
            action.setData(op["email"])
            action.triggered.connect(lambda checked, e=op["email"]: self._email_edit.setText(e))
            menu.addAction(action)

        btn = self._btn_fetch_operators
        menu.exec_(btn.mapToGlobal(btn.rect().bottomLeft()))

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
        """① 핸드오프 ACK 버튼 — EventGateway publish 만 (HTTP 호출 제거, 2026-05-10).

        backend 가 HANDOFF_ACK 받아 비즈니스 로직 (AMR routing, pp_task_txn QUE 전이) 처리.
        결과는 ITEM_LOOKUP_RESULT 또는 별도 EventType 으로 PyQt 가 subscribe (Watch...) 받음.
        """
        try:
            from app.clients.event_gateway import publish_event as _publish_eg

            ok = _publish_eg(
                event_type="HANDOFF_ACK",
                resource_id="CONV1",
                payload={
                    "zone": "postprocessing",
                    "source_device": "pyqt-pp-worker",
                    "button": "ui-handoff",
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"① 핸드오프 publish 예외: {exc}", ok=False)
            return
        if ok:
            self._set_status(
                "① 핸드오프 ACK publish 됨 — backend 처리 후 ITEM_LOOKUP_RESULT 수신 시 정보 표시",
                ok=True,
            )
        else:
            self._set_status(
                "① 핸드오프 publish 실패 (EventGateway 비활성 또는 backend 미가동)",
                ok=False,
            )

    def _on_rfid_scan(self) -> None:
        """② RFID 스캔 버튼 — EventGateway publish ITEM_LOOKUP_REQUESTED (HTTP 제거, 2026-05-10).

        backend 가 raw_payload 받아 item + pp_options 조회 후 ITEM_LOOKUP_RESULT 로 publish.
        PyQt 는 _handle_item_lookup_result slot 에서 응답 받아 화면 표시. 상태 변경 X.
        """
        payload = self._payload()
        if not payload:
            self._set_status("RFID payload 입력 필요 (예: order_17_item_20260417_7)", ok=False)
            return
        self._current_payload = payload
        try:
            from app.clients.event_gateway import publish_event as _publish_eg

            ok = _publish_eg(
                event_type="ITEM_LOOKUP_REQUESTED",
                resource_id="CONV1",
                payload={
                    "raw_payload": payload,
                    "source_device": "pyqt-pp-worker",
                    "button": "ui-rfid-scan",
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"② RFID 조회 publish 예외: {exc}", ok=False)
            return
        if ok:
            self._set_status(
                f"② RFID 조회 publish 됨 — payload={payload}, ITEM_LOOKUP_RESULT 수신 대기",
                ok=True,
            )
        else:
            self._set_status(
                "② RFID 조회 publish 실패 (EventGateway 비활성 또는 backend 미가동)",
                ok=False,
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
        """③ 후처리 완료 — EventGateway publish PP_DONE_REQUESTED 만 (HTTP 제거, 2026-05-10).

        backend 가 PP_DONE_REQUESTED 받아 ESP32 motor on dispatch + PP→ToINSP 전이 처리.
        결과 (transition / 컨베이어 ack) 는 backend 의 별도 EventType 또는 ItemEvent stream
        으로 PyQt 가 받음 (또는 단방향 — 동료 backend 결정).
        """
        self._reset_pp_done_ui()
        try:
            from app.clients.event_gateway import publish_event as _publish_eg

            ok = _publish_eg(
                event_type="PP_DONE_REQUESTED",
                resource_id=self.SENSOR_DB_RES_ID,  # "CONV1"
                payload={
                    "source_device": "pyqt-pp-worker",
                    "button": "ui-pp-done",
                    "item_id": int(self._current_item_id) if self._current_item_id else 0,
                    "rfid_payload": self._current_payload or "",
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"③ 후처리 완료 publish 예외: {exc}", ok=False)
            return
        if ok:
            self._set_status(
                f"③ 후처리 완료 publish 됨 — backend 가 PP→ToINSP 전이 + 컨베이어 모터 ON",
                ok=True,
            )
        else:
            self._set_status(
                "③ 후처리 완료 publish 실패 (EventGateway 비활성 또는 backend 미가동)",
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

    # ---- EventGateway WatchEvents subscribe (2026-05-10) — HTTP polling 대체 ----
    def _setup_event_gateway_subscribe(self) -> None:
        """EventGateway WatchEvents stream 등록 — RFID_SCANNED / TOF1_ENTRY / ITEM_LOOKUP_RESULT.

        QThread + pyqtSignal(dict) → main thread slot. GUI 변경은 slot 에서.
        EVENT_GATEWAY_TARGET 미설정 시 silent skip.
        """
        try:
            from app.clients.event_gateway import WatchEventsThread
        except Exception:  # noqa: BLE001
            return
        try:
            self._eg_watcher = WatchEventsThread(
                event_types=["RFID_SCANNED", "ITEM_LOOKUP_RESULT"],
                consumer="pyqt-pp-worker",
            )
            self._eg_watcher.event_received.connect(self._on_event_gateway_event)
            self._eg_watcher.start()
        except Exception:  # noqa: BLE001 — 비활성 또는 셋업 실패는 silent (UX 영향 X)
            self._eg_watcher = None

    def _on_event_gateway_event(self, decoded: dict) -> None:
        """WatchEvents stream 이 emit 한 단일 이벤트 — main thread slot.

        decoded keys: event_type, resource_id, source, idempotency_key, payload.
        EventType 별 분기 후 GUI 위젯 갱신.
        """
        et = decoded.get("event_type")
        payload = decoded.get("payload") or {}
        if et == "RFID_SCANNED":
            self._handle_rfid_scanned_event(payload)
        elif et == "ITEM_LOOKUP_RESULT":
            self._handle_item_lookup_result(payload)

    def _handle_rfid_scanned_event(self, payload: dict) -> None:
        """RFID_SCANNED → _payload_edit 자동 채움.

        보호:
            - 시작 시각 이전 stale 무시 (occurred_at_unix 또는 _last_auto_payload_scanned_at).
            - 동일 raw_payload 중복 진입 무시.
        """
        raw_payload = str(payload.get("raw_payload") or "").strip()
        if not raw_payload:
            return
        # scanned_at 비교 (있으면) — 시작 이전 stale 차단
        scanned_at = payload.get("scanned_at_unix") or payload.get("scanned_at")
        if isinstance(scanned_at, (int, float)):
            if scanned_at <= self._last_auto_payload_scanned_at:
                return
            self._last_auto_payload_scanned_at = float(scanned_at)
        self._payload_edit.setText(raw_payload)
        self._payload_edit.repaint()
        self._set_status(
            f"RFID 자동 입력: {raw_payload} — ② RFID 스캔 버튼을 눌러 정보 조회",
            ok=True,
        )

    def _handle_item_lookup_result(self, payload: dict) -> None:
        """ITEM_LOOKUP_RESULT → ② RFID 스캔 버튼 응답 (item + pp_options) 표시."""
        item = payload.get("item") or {}
        pp_options = payload.get("pp_options") or []
        if not item:
            self._set_status(
                f"② RFID 매칭 실패 — payload={self._current_payload}",
                ok=False,
            )
            return
        self._render_item(item)
        self._render_options(pp_options)
        self._current_item_id = item.get("item_id")
        self._set_status(
            f"② RFID 조회 OK — 제품 #{item.get('item_id')} "
            f"· 현재 단계: {item.get('cur_stat')} · 옵션 {len(pp_options)}건 "
            "(상태 변경은 ③ 후처리 완료 시점)"
        )

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
