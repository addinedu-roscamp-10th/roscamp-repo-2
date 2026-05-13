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
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.api_client import ApiClient
from app.management_client import ManagementClient
from app.widgets.alert_widgets import AlertListItem
from app.widgets.factory_map import FactoryMapView

logger = logging.getLogger(__name__)

_ACTIVE_EQUIP_STATS = {"IDLE", "ALLOC", "MV_SRC", "GRASP", "MV_DEST", "RELEASE", "TO_IDLE", "ON"}

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

    def __init__(self, api: ApiClient, mgmt: ManagementClient) -> None:
        super().__init__()
        self._api = api
        self._mgmt = mgmt
        self._kpi_cards: dict[str, KpiCard] = {}
        self._zone_rows: dict[str, _ZoneRow] = {}
        self._amr_rows: dict[str, _AmrRow] = {}
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # 제목
        title = QLabel("대시보드")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        # KPI 그리드 (4칸)
        kpi_grid = QGridLayout()
        kpi_grid.setSpacing(14)
        kpi_items = [
            ("active_orders", "금일 생산 진행 주문", "건"),
            ("production_rate", "금일 생산량", "개"),
            ("defect_rate", "불량률", "%"),
            ("oee", "설비 가동률", "%"),
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

        self._map = FactoryMapView()
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

        layout.addLayout(map_row, stretch=1)

        # 알림 목록 (최하단)
        alert_title = QLabel("실시간 알림")
        alert_title.setObjectName("sectionTitle")
        layout.addWidget(alert_title)

        self._alert_scroll = QScrollArea()
        self._alert_scroll.setObjectName("alertScroll")
        self._alert_scroll.setWidgetResizable(True)
        self._alert_scroll.setFrameShape(QFrame.NoFrame)
        self._alert_scroll.setMaximumHeight(200)

        self._alert_container = QWidget()
        self._alert_layout = QVBoxLayout(self._alert_container)
        self._alert_layout.setContentsMargins(0, 0, 0, 0)
        self._alert_layout.setSpacing(6)
        self._alert_layout.addStretch()

        self._alert_scroll.setWidget(self._alert_container)
        layout.addWidget(self._alert_scroll)

    def refresh(self) -> None:
        """Management Service(gRPC) 에서 KPI 갱신."""
        self._refresh_kpi()
        self._refresh_zone_panel()
        self._refresh_amr_panel()
        self._refresh_alerts()

        # 공장 맵 (기존 REST 유지)
        equipment = self._api.get_equipment_raw() or []
        self._map.update_equipment(equipment)

    def _refresh_kpi(self) -> None:
        try:
            mfg_orders = self._mgmt.list_production_orders(status_filters=["MFG"])
        except grpc.RpcError as e:
            logger.warning("list_production_orders 실패: %s", e)
            return

        active_order_count = len(mfg_orders)

        all_items: list[dict[str, Any]] = []
        for order in mfg_orders:
            try:
                items = self._mgmt.list_item_views(
                    order_id=str(order["ord_id"]), limit=500
                )
                all_items.extend(items)
            except grpc.RpcError as e:
                logger.warning("list_item_views ord_id=%s 실패: %s", order["ord_id"], e)

        inspected = [i for i in all_items if i.get("is_defective") is not None]
        defective_count = sum(1 for i in inspected if i["is_defective"])
        good_count = len(inspected) - defective_count
        defect_rate = (defective_count / len(inspected) * 100) if inspected else 0.0

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

    def _refresh_zone_panel(self) -> None:
        try:
            stages = self._mgmt.list_stages()
        except grpc.RpcError as e:
            logger.warning("list_stages 실패: %s", e)
            return
        count_map = {s["zone_nm"]: s["in_progress_count"] for s in stages}
        for zone_nm, row in self._zone_rows.items():
            row.update_count(count_map.get(zone_nm, 0))

    def _refresh_amr_panel(self) -> None:
        _TAT_IDS = ["TAT1", "TAT2", "TAT3"]

        try:
            robots = self._mgmt.get_robot_status()
            amrs = {r["id"]: r for r in robots if r.get("type") == "amr"}
        except grpc.RpcError as e:
            logger.warning("get_robot_status 실패: %s", e)
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

    def handle_ws_message(self, payload: dict[str, Any]) -> None:
        if payload.get("type") in (
            "dashboard_update",
            "order_update",
            "production_update",
        ):
            self.refresh()

    def handle_mqtt_message(self, topic: str, payload: dict[str, Any]) -> None:
        pass
