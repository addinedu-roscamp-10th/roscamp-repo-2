"""대시보드 페이지 - KPI 카드 + 공장 현황 맵 + 실시간 알림."""

from __future__ import annotations

import logging
from typing import Any

import grpc
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.management_client import ManagementClient
from app.widgets.alert_widgets import AlertListItem
from app.widgets.factory_map import FactoryMapView

logger = logging.getLogger(__name__)

_ACTIVE_EQUIP_STATS = {"IDLE", "ALLOC", "MV_SRC", "GRASP", "MV_DEST", "RELEASE", "TO_IDLE", "ON"}

# 패턴 번호 → 표시 라벨. PatternControlPage 의 _PATTERN_LABEL 과 동의어 (간략 버전).
_PATTERN_LABEL: dict[int, str] = {1: "원형", 2: "사각", 3: "타원"}

_ZONE_LABELS: dict[str, str] = {
    "CAST": "주조",
    "PP": "후처리",
    "INSP": "검사",
    "STRG": "적재",
    "PICK": "피킹",
    "SHIP": "출하",
    "CHG": "충전",
}

_AMR_STATUS_DOT: dict[str, str] = {
    "active":   "●",
    "idle":     "●",
    "warning":  "●",
    "error":    "●",
    "charging": "●",
}
_AMR_STATUS_COLOR: dict[str, str] = {
    "active":   "#4ade80",
    "idle":     "#9ca3af",
    "warning":  "#fbbf24",
    "error":    "#f87171",
    "charging": "#60a5fa",
}


def _amr_status_key(raw: str) -> str:
    s = (raw or "").lower()
    return {
        "running": "active", "active": "active", "moving": "active",
        "idle": "idle", "waiting": "idle",
        "warning": "warning",
        "error": "error", "fault": "error",
        "charging": "charging", "chg": "charging",
    }.get(s, "idle")


class KpiCard(QFrame):
    """KPI 표시 카드."""

    def __init__(self, title: str, value: str = "-", unit: str = "") -> None:
        super().__init__()
        self.setObjectName("kpiCard")
        self.setFrameShape(QFrame.StyledPanel)

        self._title = QLabel(title)
        self._title.setObjectName("kpiTitle")

        self._value = QLabel(value)
        self._value.setObjectName("kpiValue")

        self._unit = QLabel(unit)
        self._unit.setObjectName("kpiUnit")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(6)
        layout.addWidget(self._title)

        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        bottom.addWidget(self._value, alignment=Qt.AlignBottom)
        bottom.addWidget(self._unit, alignment=Qt.AlignBottom)
        bottom.addStretch()
        layout.addLayout(bottom)

    def update_value(self, value: str | int | float, unit: str = "") -> None:
        self._value.setText(str(value))
        if unit:
            self._unit.setText(unit)


class _ZoneRow(QWidget):
    """구역 현황 한 행."""

    def __init__(self, zone_nm: str) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)

        label = QLabel(_ZONE_LABELS.get(zone_nm, zone_nm))
        label.setObjectName("zoneRowLabel")
        label.setFixedWidth(52)

        self._count = QLabel("0건")
        self._count.setObjectName("zoneRowCount")
        self._count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        lay.addWidget(label)
        lay.addStretch()
        lay.addWidget(self._count)

    def update_count(self, n: int) -> None:
        self._count.setText(f"{n}건")


class _AmrRow(QWidget):
    """AMR 상태 한 행."""

    def __init__(self, amr_id: str) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(14)
        self._dot.setAlignment(Qt.AlignCenter)

        self._id_label = QLabel(amr_id)
        self._id_label.setObjectName("amrRowId")

        self._bat = QLabel("-")
        self._bat.setObjectName("amrRowBat")
        self._bat.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        lay.addWidget(self._dot)
        lay.addWidget(self._id_label, stretch=1)
        lay.addWidget(self._bat)

    def update(self, status: str, battery: float) -> None:
        key = _amr_status_key(status)
        color = _AMR_STATUS_COLOR.get(key, "#9ca3af")
        self._dot.setStyleSheet(f"color: {color};")
        self._bat.setText(f"{int(battery)}%")

    def set_offline(self) -> None:
        self._dot.setStyleSheet("color: #d1d5db;")
        self._bat.setText("미가동")


class DashboardPage(QWidget):
    """대시보드 페이지 - KPI + 공장 현황 맵 (좌우 패널 포함) + 알림."""

    def __init__(self, mgmt: ManagementClient) -> None:
        super().__init__()
        self._mgmt = mgmt
        self._kpi_cards: dict[str, KpiCard] = {}
        self._zone_rows: dict[str, _ZoneRow] = {}
        self._amr_rows: dict[str, _AmrRow] = {}
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        # 2026-05-14: 페이지 전체를 QScrollArea 로 감싸 상하 스크롤 가능하게.
        # 공장 현황 맵 row 가 고정 600px 라 KPI/알림/생산목록과 함께 윈도우보다 길어질 수 있음.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("dashboardScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # 제목
        title = QLabel("대시보드")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        # KPI 그리드 (4칸)
        kpi_grid = QGridLayout()
        kpi_grid.setSpacing(14)
        # 2026-05-14: 사용자 요청 순서 — 생산량 → 불량률 → 설비가동률 → 진행 주문.
        kpi_items = [
            ("production_rate", "금일 생산량", "개"),
            ("defect_rate", "불량률", "%"),
            ("oee", "설비 가동률", "%"),
            ("active_orders", "금일 진행 주문", "건"),
        ]
        for idx, (key, title_text, unit) in enumerate(kpi_items):
            card = KpiCard(title_text, unit=unit)
            self._kpi_cards[key] = card
            kpi_grid.addWidget(card, 0, idx)
        layout.addLayout(kpi_grid)

        # 맵 행: [구역 현황] | [공장 현황 맵] | [AMR 상태]
        map_row = QHBoxLayout()
        map_row.setSpacing(12)

        # --- 왼쪽: 구역 현황 패널 ---
        zone_panel = QFrame()
        zone_panel.setObjectName("tableCard")
        zone_panel.setFixedWidth(150)
        zone_layout = QVBoxLayout(zone_panel)
        zone_layout.setContentsMargins(12, 10, 12, 10)
        zone_layout.setSpacing(4)

        zone_title = QLabel("구역 현황")
        zone_title.setObjectName("sectionTitle")
        zone_layout.addWidget(zone_title)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("panelSep")
        zone_layout.addWidget(sep)

        for zone_nm in ("CAST", "PP", "INSP", "STRG", "PICK", "SHIP", "CHG"):
            row = _ZoneRow(zone_nm)
            self._zone_rows[zone_nm] = row
            zone_layout.addWidget(row)

        zone_layout.addStretch()
        map_row.addWidget(zone_panel)

        # --- 중앙: 공장 현황 맵 ---
        map_container = QFrame()
        map_container.setObjectName("tableCard")
        map_layout = QVBoxLayout(map_container)
        map_layout.setContentsMargins(12, 10, 12, 12)
        map_layout.setSpacing(6)

        map_title = QLabel("공장 현황 맵")
        map_title.setObjectName("sectionTitle")
        map_layout.addWidget(map_title)

        self._map = FactoryMapView(enable_sim=False)
        map_layout.addWidget(self._map, stretch=1)
        map_row.addWidget(map_container, stretch=1)

        # --- 오른쪽: AMR 상태 패널 ---
        amr_panel = QFrame()
        amr_panel.setObjectName("tableCard")
        amr_panel.setFixedWidth(150)
        amr_layout = QVBoxLayout(amr_panel)
        amr_layout.setContentsMargins(12, 10, 12, 10)
        amr_layout.setSpacing(4)

        amr_title = QLabel("TAT 상태")
        amr_title.setObjectName("sectionTitle")
        amr_layout.addWidget(amr_title)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setObjectName("panelSep")
        amr_layout.addWidget(sep2)

        self._amr_list_layout = QVBoxLayout()
        self._amr_list_layout.setSpacing(4)
        amr_layout.addLayout(self._amr_list_layout)
        amr_layout.addStretch()
        map_row.addWidget(amr_panel)

        # 2026-05-14: 공장 현황 맵 영역을 고정 높이로. 너무 작아 보이지 않게 600px 보장.
        # 외곽 QScrollArea 가 있으므로 페이지가 더 길어져도 스크롤로 처리됨.
        map_row_widget = QWidget()
        map_row_widget.setLayout(map_row)
        map_row_widget.setFixedHeight(600)
        layout.addWidget(map_row_widget)

        # 알림 목록 (최하단)
        alert_title = QLabel("실시간 알림")
        alert_title.setObjectName("sectionTitle")
        layout.addWidget(alert_title)

        # 2026-05-14: 내부 QScrollArea 제거 — 페이지 외곽 스크롤이 처리, 모든 알림을 풀로 노출.
        self._alert_container = QWidget()
        self._alert_layout = QVBoxLayout(self._alert_container)
        self._alert_layout.setContentsMargins(0, 0, 0, 0)
        self._alert_layout.setSpacing(6)
        self._alert_layout.addStretch()
        layout.addWidget(self._alert_container)

        # 생산 중인 목록 (MFG 상태 주문)
        prod_title = QLabel("생산 중인 목록")
        prod_title.setObjectName("sectionTitle")
        layout.addWidget(prod_title)

        self._prod_list = QTableWidget(0, 6)
        self._prod_list.setObjectName("productionListTable")
        self._prod_list.setHorizontalHeaderLabels(
            ["주문 ID", "고객사", "패턴", "목표", "완료", "진행률"]
        )
        self._prod_list.verticalHeader().setVisible(False)
        self._prod_list.setEditTriggers(QTableWidget.NoEditTriggers)
        self._prod_list.setSelectionBehavior(QTableWidget.SelectRows)
        self._prod_list.setAlternatingRowColors(True)
        # 2026-05-14: 내부 세로 스크롤 제거 — 페이지 외곽 스크롤이 처리,
        # _refresh_production_list 가 row 합산으로 setFixedHeight 동적 조정.
        self._prod_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        prod_header = self._prod_list.horizontalHeader()
        prod_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        prod_header.setSectionResizeMode(1, QHeaderView.Stretch)
        prod_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        prod_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        prod_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        prod_header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        layout.addWidget(self._prod_list)

    def refresh(self) -> None:
        """Management Service(gRPC) 에서 KPI + 진행 목록 갱신."""
        production_summary = self._refresh_kpi()
        self._refresh_zone_panel()
        robots = self._refresh_amr_panel()
        self._refresh_alerts()
        self._refresh_production_list(production_summary)

        # 공장 맵: gRPC 실데이터 (TAT AMCL 위치 + PAT/MAT 상태)
        try:
            equipment = self._mgmt.list_equipment()
        except Exception:
            equipment = []
        self._map.update_robots(robots, equipment)

    def _refresh_kpi(self) -> list[dict[str, Any]]:
        """KPI 갱신 + 진행 목록 표시용 ord_id 별 요약 반환.

        반환 형식: [{ord_id, customer_name, target, completed, defective}, ...]
        backend gRPC 호출 실패 시 빈 리스트 (mock fallback 없음 — UI 가 비어 보이게).
        """
        summary: list[dict[str, Any]] = []
        try:
            mfg_orders = self._mgmt.list_production_orders(status_filters=["MFG"])
        except grpc.RpcError as e:
            logger.warning("list_production_orders 실패: %s", e)
            return summary

        active_order_count = len(mfg_orders)

        all_items: list[dict[str, Any]] = []
        for order in mfg_orders:
            try:
                items = self._mgmt.list_item_views(
                    order_id=str(order["ord_id"]), limit=500
                )
            except grpc.RpcError as e:
                logger.warning("list_item_views ord_id=%s 실패: %s", order["ord_id"], e)
                items = []
            all_items.extend(items)

            inspected = [i for i in items if i.get("is_defective") is not None]
            defective = sum(1 for i in inspected if i["is_defective"])
            good = len(inspected) - defective
            summary.append(
                {
                    "ord_id": order["ord_id"],
                    "customer_name": order.get("customer_name") or order.get("company_name") or "-",
                    "target": int(order.get("total_amount") or 0),
                    "completed": good,
                    "defective": defective,
                }
            )

        inspected_all = [i for i in all_items if i.get("is_defective") is not None]
        defective_count = sum(1 for i in inspected_all if i["is_defective"])
        good_count = len(inspected_all) - defective_count
        defect_rate = (defective_count / len(inspected_all) * 100) if inspected_all else 0.0

        try:
            equipment_list = self._mgmt.list_equipment()
            active_count = sum(
                1 for e in equipment_list if e.get("cur_stat") in _ACTIVE_EQUIP_STATS
            )
            oee = (active_count / len(equipment_list) * 100) if equipment_list else 0.0
        except grpc.RpcError as e:
            logger.warning("list_equipment 실패: %s", e)
            oee = 0.0

        self._kpi_cards["production_rate"].update_value(good_count)
        self._kpi_cards["defect_rate"].update_value(f"{defect_rate:.1f}")
        self._kpi_cards["oee"].update_value(f"{oee:.1f}")
        self._kpi_cards["active_orders"].update_value(active_order_count)

        return summary

    def _refresh_production_list(self, summary: list[dict[str, Any]]) -> None:
        """진행 중 주문 테이블 갱신. summary 는 _refresh_kpi 가 만든 ord_id 별 요약."""
        try:
            patterns = self._mgmt.list_patterns()
        except grpc.RpcError as e:
            logger.warning("list_patterns 실패: %s", e)
            patterns = []
        pattern_by_ord = {p["ord_id"]: p for p in patterns}

        self._prod_list.setRowCount(0)
        for row_idx, item in enumerate(summary):
            ord_id = item["ord_id"]
            pat = pattern_by_ord.get(ord_id) or {}
            pat_id = pat.get("pattern_id")
            pat_label = _PATTERN_LABEL.get(pat_id, "-") if pat_id else "-"
            target = item["target"]
            completed = item["completed"]
            ratio = (completed / target * 100) if target else 0.0

            self._prod_list.insertRow(row_idx)
            self._prod_list.setItem(row_idx, 0, QTableWidgetItem(str(ord_id)))
            self._prod_list.setItem(row_idx, 1, QTableWidgetItem(str(item["customer_name"])))
            self._prod_list.setItem(row_idx, 2, QTableWidgetItem(pat_label))

            target_item = QTableWidgetItem(str(target))
            target_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._prod_list.setItem(row_idx, 3, target_item)

            completed_item = QTableWidgetItem(str(completed))
            completed_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._prod_list.setItem(row_idx, 4, completed_item)

            ratio_item = QTableWidgetItem(f"{ratio:.1f}%")
            ratio_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._prod_list.setItem(row_idx, 5, ratio_item)

        # 모든 row 가 보이도록 테이블 높이 동적 조정 (내부 스크롤 사용 안 함).
        self._prod_list.resizeRowsToContents()
        total_h = self._prod_list.horizontalHeader().height() + 4
        for r in range(self._prod_list.rowCount()):
            total_h += self._prod_list.rowHeight(r)
        self._prod_list.setFixedHeight(max(80, total_h))

    def _refresh_zone_panel(self) -> None:
        try:
            stages = self._mgmt.list_stages()
        except grpc.RpcError as e:
            logger.warning("list_stages 실패: %s", e)
            return
        count_map = {s["zone_nm"]: s["in_progress_count"] for s in stages}
        for zone_nm, row in self._zone_rows.items():
            row.update_count(count_map.get(zone_nm, 0))

    def _refresh_amr_panel(self) -> list[dict]:
        """AMR 상태 패널 갱신. gRPC 로봇 목록을 반환 (맵 갱신에 재사용)."""
        _TAT_IDS = ["TAT1", "TAT2", "TAT3"]

        try:
            robots = self._mgmt.get_robot_status()
            amrs = {r["id"]: r for r in robots if r.get("type") == "amr"}
        except grpc.RpcError as e:
            logger.warning("get_robot_status 실패: %s", e)
            robots = []
            amrs = {}

        # 기존 위젯 제거
        while self._amr_list_layout.count():
            item = self._amr_list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._amr_rows.clear()

        for tat_id in _TAT_IDS:
            row = _AmrRow(tat_id)
            if tat_id in amrs:
                row.update(amrs[tat_id].get("status", ""), amrs[tat_id].get("battery", 0))
            else:
                row.set_offline()
            self._amr_rows[tat_id] = row
            self._amr_list_layout.addWidget(row)

        return robots

    def _refresh_alerts(self) -> None:
        try:
            alerts = self._mgmt.list_alerts(limit=20)
        except grpc.RpcError as e:
            logger.warning("list_alerts 실패: %s", e)
            return
        self._rebuild_alerts(alerts)
        self._emit_critical_alerts(alerts)

    def _emit_critical_alerts(self, alerts: list[dict[str, Any]]) -> None:
        win = self.window()
        show_toast = getattr(win, "_maybe_show_toast_for_alert", None)
        if show_toast is None:
            return
        for alert in alerts[:5]:
            show_toast(alert)

    def _rebuild_alerts(self, alerts: list[dict[str, Any]]) -> None:
        while self._alert_layout.count() > 1:
            item = self._alert_layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()
        for alert in alerts:
            widget = AlertListItem(alert)
            self._alert_layout.insertWidget(self._alert_layout.count() - 1, widget)
