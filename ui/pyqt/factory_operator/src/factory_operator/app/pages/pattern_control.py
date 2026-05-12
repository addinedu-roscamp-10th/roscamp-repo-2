"""패턴 위치 조작 및 생산 시작 페이지.

UI 구성 (템플릿 기준):
  1) 상단 통계 카드 — 패턴 미등록 / 생산 승인 / 금일 생산 주문
  2) 패턴 등록 섹션 — 미등록 주문 목록(좌) + 주문번호/패턴위치 입력(우)
  3) 생산 계획 섹션 — 등록 주문 다중선택(좌) + 우선순위 결과표(우)
                     하단: [우선 순위계산]  [생산 시작 (파란색)]
"""

from __future__ import annotations

from typing import Any

import sip
from PyQt5.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.api_client import ApiClient


# ── 상단 통계 카드 ────────────────────────────────────────────────────────────

class _StatBox(QFrame):
    """숫자 + 레이블 한 쌍의 통계 카드."""

    def __init__(self, label: str, highlight: bool = False) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Box)
        self.setFrameShadow(QFrame.Raised)
        self.setLineWidth(2)
        self.setMinimumHeight(90)

        border_color = "#e53935" if highlight else "#888"
        self.setStyleSheet(
            f"QFrame {{ border: 2px solid {border_color}; border-radius: 6px; background: transparent; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(6)

        self._value_lbl = QLabel("—")
        value_font = QFont()
        value_font.setPointSize(24)
        value_font.setBold(True)
        self._value_lbl.setFont(value_font)
        self._value_lbl.setAlignment(Qt.AlignCenter)
        if highlight:
            self._value_lbl.setStyleSheet("color: #e53935; border: none;")
        else:
            self._value_lbl.setStyleSheet("border: none;")

        lbl = QLabel(label)
        lbl_font = QFont()
        lbl_font.setPointSize(11)
        lbl.setFont(lbl_font)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("border: none;")

        layout.addWidget(self._value_lbl)
        layout.addWidget(lbl)

    def set_value(self, n: int) -> None:
        self._value_lbl.setText(f"{n}건")


# ── 백그라운드 데이터 조회 워커 ───────────────────────────────────────────────

class _RefreshWorker(QObject):
    """주문 목록 + 패턴 맵을 백그라운드에서 조회."""

    data_ready = pyqtSignal(dict)

    @pyqtSlot()
    def run(self) -> None:
        from app.management_client import ManagementClient

        def _safe(fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs) or []
            except Exception:  # noqa: BLE001
                return []

        client = ManagementClient()
        try:
            data: dict[str, Any] = {
                "orders": _safe(client.list_production_orders),
                "patterns": _safe(client.list_patterns),
            }
        finally:
            client.close()
        self.data_ready.emit(data)


# ── 메인 페이지 ───────────────────────────────────────────────────────────────

class PatternControlPage(QWidget):
    """패턴 위치 수동 조작 + 생산 계획 페이지."""

    _STATUS_ALIASES = {
        "APPROVED": "APPR",
        "IN_PRODUCTION": "MFG",
        "PRODUCTION": "MFG",
        "PRODUCTION_COMPLETED": "DONE",
        "SHIPPING_READY": "SHIP",
        "SHIPPED": "SHIP",
        "COMPLETED": "COMP",
    }
    _PATTERN_LABEL = {1: "1 (원형)", 2: "2 (사각)", 3: "3 (타원형)"}

    def __init__(self, api: ApiClient) -> None:
        super().__init__()
        self._api = api
        self._orders: list[dict[str, Any]] = []
        self._patterns: dict[int, int] = {}  # ord_id → ptn_loc_id
        self._refresh_thread: QThread | None = None
        self._refresh_worker: _RefreshWorker | None = None
        # 우선순위 계산 결과: [(ord_id, score), ...]
        self._priority_result: list[tuple[int, int]] = []
        self._build_ui()
        self.refresh()

    # ── UI 구성 ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(16)

        title = QLabel("생산 계획")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        # ── 1. 통계 카드 행 ──────────────────────────────────────────────────
        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)
        self._stat_unreg = _StatBox("패턴 미등록")
        self._stat_appr = _StatBox("생산 승인")
        self._stat_today = _StatBox("금일 생산 주문", highlight=True)
        for box in (self._stat_unreg, self._stat_appr, self._stat_today):
            stats_row.addWidget(box)
        root.addLayout(stats_row)

        # ── 2. 패턴 등록 섹션 ────────────────────────────────────────────────
        pat_group = QGroupBox("패턴 등록")
        pat_h = QHBoxLayout(pat_group)
        pat_h.setSpacing(16)

        # 좌측: 미등록 주문 목록
        left_pat = QVBoxLayout()
        left_pat.addWidget(QLabel("미등록 주문 목록"))
        self._unreg_list = QListWidget()
        self._unreg_list.currentRowChanged.connect(self._on_unreg_selected)
        left_pat.addWidget(self._unreg_list)
        pat_h.addLayout(left_pat, stretch=1)

        # 우측: 주문번호 + 패턴위치 + 등록 버튼
        right_pat = QVBoxLayout()
        right_pat.setSpacing(10)

        form = QGridLayout()
        form.setSpacing(8)
        form.addWidget(QLabel("주문번호"), 0, 0)
        self._order_no_lbl = QLabel("—")
        self._order_no_lbl.setObjectName("formValueLabel")
        form.addWidget(self._order_no_lbl, 0, 1)

        form.addWidget(QLabel("패턴위치"), 1, 0)
        self._pattern_spin = QSpinBox()
        self._pattern_spin.setRange(1, 3)
        self._pattern_spin.setMinimumWidth(80)
        form.addWidget(self._pattern_spin, 1, 1)

        right_pat.addLayout(form)
        right_pat.addStretch(1)

        self._register_btn = QPushButton("패턴 등록")
        self._register_btn.setProperty("variant", "secondary")
        self._register_btn.setEnabled(False)
        self._register_btn.clicked.connect(self._on_register_pattern)
        right_pat.addWidget(self._register_btn)

        pat_h.addLayout(right_pat, stretch=1)
        root.addWidget(pat_group)

        # ── 3. 생산 계획 섹션 ────────────────────────────────────────────────
        prod_group = QGroupBox("생산 계획")
        prod_v = QVBoxLayout(prod_group)
        prod_v.setSpacing(10)

        prod_h = QHBoxLayout()
        prod_h.setSpacing(16)

        # 좌측: 패턴 등록된 주문 목록 (다중 선택)
        left_prod = QVBoxLayout()
        left_prod.addWidget(QLabel("주문 목록 (복수 선택 가능)"))
        self._reg_list = QListWidget()
        self._reg_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self._reg_list.itemSelectionChanged.connect(self._on_reg_selection_changed)
        left_prod.addWidget(self._reg_list)
        prod_h.addLayout(left_prod, stretch=1)

        # 우측: 우선순위 결과 테이블
        right_prod = QVBoxLayout()
        right_prod.addWidget(QLabel("우선순위 결과"))
        self._priority_table = QTableWidget(0, 2)
        self._priority_table.setHorizontalHeaderLabels(["주문", "계산 점수"])
        self._priority_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._priority_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._priority_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._priority_table.setSelectionMode(QAbstractItemView.NoSelection)
        right_prod.addWidget(self._priority_table)
        prod_h.addLayout(right_prod, stretch=1)

        prod_v.addLayout(prod_h)

        # 하단 버튼 행
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        self._calc_btn = QPushButton("우선 순위계산")
        self._calc_btn.setMinimumHeight(48)
        self._calc_btn.setEnabled(False)
        self._calc_btn.clicked.connect(self._on_calc_priority)
        btn_row.addWidget(self._calc_btn, stretch=1)

        self._start_btn = QPushButton("생산 시작")
        self._start_btn.setProperty("variant", "primary")
        self._start_btn.setMinimumHeight(48)
        font = QFont()
        font.setBold(True)
        font.setPointSize(12)
        self._start_btn.setFont(font)
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start_production)
        btn_row.addWidget(self._start_btn, stretch=1)

        prod_v.addLayout(btn_row)
        root.addWidget(prod_group)

        self._status_label = QLabel()
        self._status_label.setVisible(False)

        root.addStretch(1)

    # ── 데이터 갱신 ──────────────────────────────────────────────────────────

    def refresh(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return

        worker = _RefreshWorker()
        thread = QThread(self)
        self._refresh_worker = worker
        worker.moveToThread(thread)
        worker.data_ready.connect(self._on_refresh_done)
        worker.data_ready.connect(lambda _: thread.quit())
        thread.started.connect(worker.run)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_refresh_worker)
        self._refresh_thread = thread
        thread.start()

    @pyqtSlot(dict)
    def _on_refresh_done(self, data: dict) -> None:
        if self._is_ui_deleted():
            return

        raw_orders = data.get("orders") or []
        patterns = data.get("patterns") or []

        all_orders = [self._normalize_order(item) for item in raw_orders]
        appr_orders = [o for o in all_orders if o["status"] == "APPR"]

        self._patterns = {
            int(p["ord_id"]): int(p.get("ptn_loc_id", p.get("ptn_id")))
            for p in patterns
            if p.get("ord_id") is not None and p.get("ptn_loc_id", p.get("ptn_id")) is not None
        }
        self._orders = appr_orders

        # ── 통계 카드 업데이트 ─────────────────────────────────────────
        unreg_orders = [o for o in appr_orders if o["ord_id"] not in self._patterns]
        self._stat_unreg.set_value(len(unreg_orders))
        self._stat_appr.set_value(len(appr_orders))
        self._stat_today.set_value(len(all_orders))

        # ── 미등록 주문 목록 ───────────────────────────────────────────
        self._unreg_list.blockSignals(True)
        self._unreg_list.clear()
        for order in unreg_orders:
            item = QListWidgetItem(f"ord_{order['ord_id']}  (user={order.get('user_id', '-')})")
            item.setData(Qt.UserRole, order["ord_id"])
            self._unreg_list.addItem(item)
        self._unreg_list.blockSignals(False)

        self._order_no_lbl.setText("—")
        self._register_btn.setEnabled(False)

        # ── 등록 완료 주문 목록 ────────────────────────────────────────
        reg_orders = [o for o in appr_orders if o["ord_id"] in self._patterns]
        self._reg_list.blockSignals(True)
        self._reg_list.clear()
        for order in reg_orders:
            ptn = self._PATTERN_LABEL.get(self._patterns[order["ord_id"]], "?")
            item = QListWidgetItem(f"ord_{order['ord_id']}  패턴:{ptn}")
            item.setData(Qt.UserRole, order["ord_id"])
            self._reg_list.addItem(item)
        self._reg_list.blockSignals(False)

        self._priority_table.setRowCount(0)
        self._priority_result.clear()
        self._calc_btn.setEnabled(False)
        self._start_btn.setEnabled(False)

        self._status_label.setText(
            f"APPR 주문 {len(appr_orders)}건 — 패턴 미등록 {len(unreg_orders)}건 / 등록 완료 {len(reg_orders)}건"
        )

    @pyqtSlot()
    def _clear_refresh_worker(self) -> None:
        self._refresh_worker = None

    # ── 이벤트 핸들러: 패턴 등록 섹션 ────────────────────────────────────────

    @pyqtSlot(int)
    def _on_unreg_selected(self, row: int) -> None:
        if row < 0:
            self._order_no_lbl.setText("—")
            self._register_btn.setEnabled(False)
            return
        item = self._unreg_list.item(row)
        if item is None:
            return
        ord_id = item.data(Qt.UserRole)
        self._order_no_lbl.setText(f"ord_{ord_id}")
        self._register_btn.setEnabled(True)

    @pyqtSlot()
    def _on_register_pattern(self) -> None:
        row = self._unreg_list.currentRow()
        if row < 0:
            return
        item = self._unreg_list.item(row)
        if item is None:
            return
        ord_id: int = item.data(Qt.UserRole)
        ptn_loc_id = self._pattern_spin.value()
        try:
            self._register_pattern(ord_id, ptn_loc_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "패턴 등록 실패", str(exc))
            return

        QMessageBox.information(
            self,
            "패턴 등록 완료",
            f"발주 {ord_id}\n패턴 위치 {ptn_loc_id} ({self._PATTERN_LABEL.get(ptn_loc_id, '')}) 로 등록했습니다.",
        )
        self.refresh()

    # ── 이벤트 핸들러: 생산 계획 섹션 ────────────────────────────────────────

    @pyqtSlot()
    def _on_reg_selection_changed(self) -> None:
        selected = self._reg_list.selectedItems()
        self._calc_btn.setEnabled(len(selected) > 0)
        self._start_btn.setEnabled(False)
        self._priority_result.clear()
        self._priority_table.setRowCount(0)

    @pyqtSlot()
    def _on_calc_priority(self) -> None:
        """선택된 주문들의 우선순위를 계산해 테이블에 표시."""
        selected = self._reg_list.selectedItems()
        if not selected:
            return

        ord_ids = [item.data(Qt.UserRole) for item in selected]

        # 우선순위 점수: ord_id 오름차순 → 높은 점수 (단순 임시 로직, 추후 서버 연동)
        base_score = 10 * len(ord_ids)
        ranked = [(oid, base_score - 5 * i) for i, oid in enumerate(sorted(ord_ids))]
        self._priority_result = ranked

        self._priority_table.setRowCount(len(ranked))
        for row, (oid, score) in enumerate(ranked):
            ptn = self._PATTERN_LABEL.get(self._patterns.get(oid, 0), "?")
            self._priority_table.setItem(row, 0, QTableWidgetItem(f"ord_{oid}  패턴:{ptn}"))
            score_item = QTableWidgetItem(str(score))
            score_item.setTextAlignment(Qt.AlignCenter)
            self._priority_table.setItem(row, 1, score_item)

        self._start_btn.setEnabled(True)

    @pyqtSlot()
    def _on_start_production(self) -> None:
        """우선순위 순서대로 생산 시작."""
        if not self._priority_result:
            QMessageBox.warning(self, "생산 시작", "먼저 우선 순위계산을 실행하세요.")
            return

        ord_ids = [oid for oid, _ in self._priority_result]
        confirm = QMessageBox.question(
            self,
            "생산 시작 확인",
            f"주문 {len(ord_ids)}건을 우선순위 순서대로 생산 시작하겠습니까?\n"
            + "\n".join(f"  {i+1}. ord_{oid}" for i, oid in enumerate(ord_ids)),
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        from app.management_client import ManagementClient

        errors: list[str] = []
        client = ManagementClient()
        try:
            for oid in ord_ids:
                try:
                    client.start_production_one(oid)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"ord_{oid}: {exc}")
        finally:
            client.close()

        if errors:
            QMessageBox.warning(self, "일부 실패", "\n".join(errors))
        else:
            QMessageBox.information(self, "생산 시작 완료", f"{len(ord_ids)}건 생산 시작 완료.")
        self.refresh()

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    def _register_pattern(self, ord_id: int, ptn_loc_id: int) -> None:
        from app.management_client import ManagementClient

        client = ManagementClient()
        try:
            client.register_pattern(ord_id, ptn_loc_id)
        finally:
            client.close()
        self._patterns[ord_id] = ptn_loc_id

    def _normalize_order(self, item: dict[str, Any]) -> dict[str, Any]:
        ord_id = item.get("ord_id", item.get("order_id", item.get("id")))
        try:
            ord_id_int = int(ord_id)
        except (TypeError, ValueError):
            ord_id_int = 0

        raw_status = item.get("latest_stat", item.get("ord_stat", item.get("status", "RCVD")))
        status = self._normalize_status(str(raw_status or "RCVD"))
        return {
            "ord_id": ord_id_int,
            "user_id": item.get("user_id"),
            "status": status,
        }

    def _normalize_status(self, status: str) -> str:
        key = status.strip().upper()
        return self._STATUS_ALIASES.get(key, key)

    def _is_ui_deleted(self) -> bool:
        if sip.isdeleted(self):
            return True
        for widget in (
            getattr(self, "_unreg_list", None),
            getattr(self, "_reg_list", None),
            getattr(self, "_order_no_lbl", None),
            getattr(self, "_pattern_spin", None),
            getattr(self, "_register_btn", None),
            getattr(self, "_priority_table", None),
            getattr(self, "_calc_btn", None),
            getattr(self, "_start_btn", None),
            getattr(self, "_status_label", None),
        ):
            if widget is None or sip.isdeleted(widget):
                return True
        return False

    def handle_ws_message(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type", "")
        if event_type in ("order_update", "order_status_changed", "production_approved"):
            self.refresh()
