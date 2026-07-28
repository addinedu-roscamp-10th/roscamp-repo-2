"""적재 모니터링 페이지.

물류/이송 화면에서 분리한 창고 랙 현황을 전담한다.
"""

from __future__ import annotations

import logging
from typing import Any

import grpc
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QFrame,
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
from app.pages.dashboard import KpiCard
from app.widgets.warehouse_rack import WarehouseRackWidget

STATUS_TEXT = {
    "empty": "비어있음",
    "occupied": "점유",
    "reserved": "예약",
}

logger = logging.getLogger(__name__)


class StoragePage(QWidget):
    """창고 랙 적재 상태 페이지."""

    def __init__(self, mgmt: ManagementClient) -> None:
        super().__init__()
        self._mgmt = mgmt
        self._kpi_cards: dict[str, KpiCard] = {}
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        # 2026-05-14: 페이지 외곽 스크롤 (Dashboard 패턴 통일).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel("적재")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(14)
        kpi_items = [
            ("total", "전체 랙", "개"),
            ("occupied", "점유 랙", "개"),
            ("reserved", "예약 랙", "개"),
            ("occupancy_rate", "점유율", "%"),
        ]
        for key, label, unit in kpi_items:
            card = KpiCard(label, unit=unit)
            self._kpi_cards[key] = card
            kpi_row.addWidget(card, stretch=1)
        layout.addLayout(kpi_row)

        self._rack_widget = WarehouseRackWidget()
        layout.addWidget(self._rack_widget, stretch=3)

        detail_title = QLabel("랙 상세")
        detail_title.setObjectName("sectionTitle")
        layout.addWidget(detail_title)

        self._detail_table = QTableWidget(0, 6)
        self._detail_table.setHorizontalHeaderLabels(
            ["위치 ID", "행", "열", "상태", "품목 ID", "저장 시각"]
        )
        self._detail_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._detail_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._detail_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._detail_table.setMaximumHeight(220)
        layout.addWidget(self._detail_table, stretch=1)

    def refresh(self) -> None:
        try:
            locations = self._mgmt.list_warehouse_locations()
        except grpc.RpcError as exc:
            logger.warning(
                "ListWarehouseLocations 실패, 마지막 정상 화면 유지: %s",
                exc,
            )
            return
        self._rack_widget.update_racks(
            [_location_to_rack(row) for row in locations]
        )
        self._update_kpis(locations)
        self._update_detail_table(locations)

    def _update_kpis(self, locations: list[dict[str, Any]]) -> None:
        total = len(locations)
        occupied = sum(
            1
            for location in locations
            if str(location.get("status", "")).lower() == "occupied"
        )
        reserved = sum(
            1
            for location in locations
            if str(location.get("status", "")).lower() == "reserved"
        )
        occupancy_rate = occupied * 100 / total if total else 0

        self._kpi_cards["total"].update_value(total)
        self._kpi_cards["occupied"].update_value(occupied)
        self._kpi_cards["reserved"].update_value(reserved)
        self._kpi_cards["occupancy_rate"].update_value(f"{occupancy_rate:.0f}")

    def _update_detail_table(
        self,
        locations: list[dict[str, Any]],
    ) -> None:
        rows = sorted(
            locations,
            key=lambda location: (
                int(location.get("row", 0) or 0),
                int(location.get("col", 0) or 0),
                str(location.get("loc_id", "")),
            ),
        )
        self._detail_table.setRowCount(len(rows))
        for row, location in enumerate(rows):
            location_id = str(location.get("loc_id", ""))
            status = str(location.get("status", "empty")).lower()
            item_id = location.get("item_id")

            id_item = QTableWidgetItem(location_id)
            id_item.setTextAlignment(Qt.AlignCenter)
            self._detail_table.setItem(row, 0, id_item)

            self._detail_table.setItem(
                row,
                1,
                QTableWidgetItem(str(location.get("row", ""))),
            )
            self._detail_table.setItem(
                row,
                2,
                QTableWidgetItem(str(location.get("col", ""))),
            )
            status_item = QTableWidgetItem(STATUS_TEXT.get(status, status))
            status_item.setTextAlignment(Qt.AlignCenter)
            self._detail_table.setItem(row, 3, status_item)
            self._detail_table.setItem(
                row,
                4,
                QTableWidgetItem(str(item_id) if item_id is not None else "-"),
            )
            self._detail_table.setItem(
                row,
                5,
                QTableWidgetItem(str(location.get("stored_at", ""))),
            )



def _location_to_rack(location: dict[str, Any]) -> dict[str, Any]:
    row = int(location.get("row", 0) or 0)
    col = int(location.get("col", 0) or 0)
    rack_id = str((row - 1) * 6 + col) if row >= 1 and 1 <= col <= 6 else ""
    item_id = location.get("item_id")
    return {
        "id": rack_id,
        "status": str(location.get("status", "empty")).lower(),
        "content": f"Item {item_id}" if item_id is not None else "",
    }
