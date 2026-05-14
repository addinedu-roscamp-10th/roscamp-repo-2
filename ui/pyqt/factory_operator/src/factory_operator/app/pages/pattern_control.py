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
from PyQt5.QtGui import QColor, QFont
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


class _CalcPriorityWorker(QObject):
    """선택된 APPR + 패턴 등록 주문들의 우선순위를 Management Service 에서 조회."""

    result_ready = pyqtSignal(list)   # list[dict]
    error = pyqtSignal(str)

    def __init__(self, ord_ids: list[int]) -> None:
        super().__init__()
        self._ord_ids = ord_ids

    @pyqtSlot()
    def run(self) -> None:
        from app.management_client import ManagementClient

        client = ManagementClient()
        try:
            results = client.calculate_priority(self._ord_ids)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
            return
        finally:
            client.close()
        self.result_ready.emit(results)


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
        self._selected_unreg_ord_id: int | None = None  # 클릭으로 선택된 미등록 주문
        self._refresh_thread: QThread | None = None
        self._refresh_worker: _RefreshWorker | None = None
        self._calc_thread: QThread | None = None
        self._calc_worker: _CalcPriorityWorker | None = None
        # 우선순위 계산 결과: [(ord_id, total_score), ...] rank 순
        self._priority_result: list[tuple[int, float]] = []
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
        self._stat_today = _StatBox("금일 생산 진행 주문", highlight=True)
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
        self._unreg_list.itemClicked.connect(self._on_unreg_selected)
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
        self._priority_table.setSelectionMode(QAbstractItemView.MultiSelection)
        self._priority_table.setSelectionBehavior(QAbstractItemView.SelectRows)
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
        mfg_orders = [o for o in all_orders if o["status"] == "MFG"]

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
        self._stat_today.set_value(len(mfg_orders))

        # ── 미등록 주문 목록 ───────────────────────────────────────────
        self._unreg_list.blockSignals(True)
        self._unreg_list.clear()
        unreg_ord_ids = {o["ord_id"] for o in unreg_orders}
        for order in unreg_orders:
            item = QListWidgetItem(f"주문 #{order['ord_id']}  (담당: {order.get('user_id', '-')})")
            item.setData(Qt.UserRole, order["ord_id"])
            self._unreg_list.addItem(item)
        self._unreg_list.blockSignals(False)

        # 이전에 선택된 주문이 여전히 목록에 있으면 선택 상태 유지
        if self._selected_unreg_ord_id is not None and self._selected_unreg_ord_id in unreg_ord_ids:
            self._order_no_lbl.setText(f"주문 #{self._selected_unreg_ord_id}")
            self._register_btn.setEnabled(True)
        else:
            self._selected_unreg_ord_id = None
            self._order_no_lbl.setText("—")
            self._register_btn.setEnabled(False)

        # ── 등록 완료 주문 목록 ────────────────────────────────────────
        reg_orders = [o for o in appr_orders if o["ord_id"] in self._patterns]

        # refresh 전 선택 상태 저장 → 재구성 후 복원
        prev_selected_ids = {
            self._reg_list.item(i).data(Qt.UserRole)
            for i in range(self._reg_list.count())
            if self._reg_list.item(i).isSelected()
        }

        self._reg_list.blockSignals(True)
        self._reg_list.clear()
        for order in reg_orders:
            ptn = self._PATTERN_LABEL.get(self._patterns[order["ord_id"]], "?")
            item = QListWidgetItem(f"주문 #{order['ord_id']}  패턴:{ptn}")
            item.setData(Qt.UserRole, order["ord_id"])
            self._reg_list.addItem(item)

        # 이전에 선택된 항목 복원
        for i in range(self._reg_list.count()):
            it = self._reg_list.item(i)
            if it and it.data(Qt.UserRole) in prev_selected_ids:
                it.setSelected(True)
        self._reg_list.blockSignals(False)

        # 복원된 선택이 없을 때만 결과 초기화
        restored = bool(prev_selected_ids & {o["ord_id"] for o in reg_orders})
        if restored:
            # 선택이 복원됐으면 calc 버튼은 항상 활성화 (start 는 calc 결과에 따라 유지)
            self._calc_btn.setEnabled(True)
        else:
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

    @pyqtSlot(QListWidgetItem)
    def _on_unreg_selected(self, item: QListWidgetItem) -> None:
        if item is None:
            return
        ord_id = item.data(Qt.UserRole)
        self._selected_unreg_ord_id = ord_id
        self._order_no_lbl.setText(f"주문 #{ord_id}")
        self._register_btn.setEnabled(True)

    @pyqtSlot()
    def _on_register_pattern(self) -> None:
        ord_id = self._selected_unreg_ord_id
        if ord_id is None:
            return
        ptn_loc_id = self._pattern_spin.value()
        try:
            self._register_pattern(ord_id, ptn_loc_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "패턴 등록 실패", str(exc))
            return

        self._selected_unreg_ord_id = None
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
        has_selection = len(selected) > 0
        self._calc_btn.setEnabled(has_selection)
        # 선택이 바뀌면 이전 계산 결과는 항상 초기화 (재계산 필요)
        self._start_btn.setEnabled(False)
        self._priority_result.clear()
        self._priority_table.setRowCount(0)

    @pyqtSlot()
    def _on_calc_priority(self) -> None:
        """선택된 APPR + 패턴 등록 주문을 Management Service 에 보내 우선순위 계산."""
        if self._calc_thread is not None and self._calc_thread.isRunning():
            return

        selected = self._reg_list.selectedItems()
        if not selected:
            return

        ord_ids: list[int] = [item.data(Qt.UserRole) for item in selected]

        self._calc_btn.setEnabled(False)
        self._start_btn.setEnabled(False)
        self._priority_table.setRowCount(0)
        self._priority_result.clear()

        worker = _CalcPriorityWorker(ord_ids)
        thread = QThread(self)
        self._calc_worker = worker
        worker.moveToThread(thread)
        worker.result_ready.connect(self._on_calc_done)
        worker.error.connect(self._on_calc_error)
        worker.result_ready.connect(lambda _: thread.quit())
        worker.error.connect(lambda _: thread.quit())
        thread.started.connect(worker.run)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_calc_worker)
        self._calc_thread = thread
        thread.start()

    @pyqtSlot(list)
    def _on_calc_done(self, results: list) -> None:
        """Management Service 응답 → 우선순위 테이블 업데이트."""
        if self._is_ui_deleted():
            return

        if not results:
            QMessageBox.warning(self, "우선 순위계산", "계산 결과가 없습니다.\n선택한 주문의 상태를 확인하세요.")
            self._calc_btn.setEnabled(True)
            return

        # _priority_result: [(ord_id, total_score), ...] rank 순(1 = 최우선)
        self._priority_result = [(r["order_id"], r["total_score"]) for r in results]

        self._priority_table.setRowCount(len(results))
        self._priority_table.setVerticalHeaderLabels(
            [f"{r['rank']}위" for r in results]
        )
        delay_color = {"high": "#e53935", "medium": "#fb8c00", "low": ""}
        for row, r in enumerate(results):
            oid = r["order_id"]
            ptn = self._PATTERN_LABEL.get(self._patterns.get(oid, 0), "?")
            label_item = QTableWidgetItem(f"  주문 #{oid}  패턴:{ptn}")
            color = delay_color.get(r["delay_risk"], "")
            if color:
                label_item.setForeground(QColor(color))
            self._priority_table.setItem(row, 0, label_item)

            score_item = QTableWidgetItem(str(r["total_score"]))
            score_item.setTextAlignment(Qt.AlignCenter)
            self._priority_table.setItem(row, 1, score_item)

        self._start_btn.setEnabled(True)
        self._calc_btn.setEnabled(True)

    @pyqtSlot(str)
    def _on_calc_error(self, msg: str) -> None:
        if self._is_ui_deleted():
            return
        QMessageBox.critical(self, "우선 순위계산 실패", msg)
        self._calc_btn.setEnabled(True)

    @pyqtSlot()
    def _clear_calc_worker(self) -> None:
        self._calc_worker = None

    @pyqtSlot()
    def _on_start_production(self) -> None:
        """우선순위 테이블에서 선택한 주문만 생산 시작."""
        if not self._priority_result:
            QMessageBox.warning(self, "생산 시작", "먼저 우선 순위계산을 실행하세요.")
            return

        # 테이블에서 선택된 행 번호 수집 (중복 제거, rank 순 유지)
        selected_rows = sorted({idx.row() for idx in self._priority_table.selectedIndexes()})
        if not selected_rows:
            QMessageBox.warning(self, "생산 시작", "우선순위 결과 테이블에서 시작할 주문을 선택하세요.")
            return

        ord_ids = [
            self._priority_result[row][0]
            for row in selected_rows
            if row < len(self._priority_result)
        ]
        confirm = QMessageBox.question(
            self,
            "생산 시작 확인",
            f"주문 {len(ord_ids)}건을 우선순위 순서대로 생산 시작하겠습니까?\n"
            + "\n".join(f"  {i+1}. 주문 #{oid}" for i, oid in enumerate(ord_ids)),
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
                    errors.append(f"주문 #{oid}: {exc}")
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
