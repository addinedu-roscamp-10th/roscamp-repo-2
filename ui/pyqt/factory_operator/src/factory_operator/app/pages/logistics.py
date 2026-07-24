"""물류/이송 모니터링 페이지 v0.4.

구성:
  1. AMR 상태 카드 (3대, 배터리 바 포함)
  2. 이송 작업 큐 (우선순위 배지)
  3. 출고 지시 테이블
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
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

from app.management_client import ManagementClient
from app.pages.dashboard import KpiCard
from app.widgets.amr_card import AmrStatusCard

TASK_STATUS_TEXT = {
    "QUE": "대기",
    "PROC": "진행 중",
    "SUCC": "완료",
    "FAIL": "실패",
}

ORDER_STATUS_TEXT = {
    "DONE": "생산 완료",
    "SHIPPING": "출고 중",
    "COMP": "완료",
}


class LogisticsPage(QWidget):
    def __init__(self, mgmt: ManagementClient) -> None:
        super().__init__()
        self._mgmt = mgmt
        self._amr_cards: dict[str, AmrStatusCard] = {}
        self._kpi_cards: dict[str, KpiCard] = {}
        self._amr_live: list[dict[str, Any]] = []
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

        title = QLabel("물류 / 이송")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        # KPI 4개 카드 (물류 요약)
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(14)
        kpi_items = [
            ("active_tasks", "진행 중 작업", "건"),
            ("pending_tasks", "대기 작업", "건"),
            ("completed_tasks", "완료 작업", "건"),
            ("idle_amr", "대기 중 AMR", "대"),
        ]
        for key, label, unit in kpi_items:
            card = KpiCard(label, unit=unit)
            self._kpi_cards[key] = card
            kpi_row.addWidget(card, stretch=1)
        layout.addLayout(kpi_row)

        # 1) AMR 카드 (최대 3대 가로 배치)
        amr_label = QLabel("AMR 현황")
        amr_label.setObjectName("sectionTitle")
        layout.addWidget(amr_label)

        self._amr_row = QHBoxLayout()
        self._amr_row.setSpacing(14)

        amr_container = QWidget()
        amr_container.setLayout(self._amr_row)
        amr_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(amr_container)

        # 2) 중단: 이송 작업 큐
        task_card = QFrame()
        task_card.setObjectName("tableCard")
        task_card.setFrameShape(QFrame.StyledPanel)
        task_layout = QVBoxLayout(task_card)
        task_layout.setContentsMargins(12, 10, 12, 12)
        task_layout.setSpacing(8)

        task_title = QLabel("이송 작업 큐")
        task_title.setObjectName("sectionTitle")
        task_layout.addWidget(task_title)

        self._task_table = QTableWidget(0, 7)
        self._task_table.setHorizontalHeaderLabels(
            [
                "작업 ID",
                "작업 유형",
                "담당 AMR",
                "제품 ID",
                "주문 ID",
                "상태",
                "요청 시각",
            ]
        )
        self._task_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._task_table.verticalHeader().setVisible(False)
        self._task_table.setEditTriggers(QTableWidget.NoEditTriggers)
        task_layout.addWidget(self._task_table)
        layout.addWidget(task_card, stretch=1)

        # 3) 하단: 출고 지시 테이블
        outbound_label = QLabel("출고 지시")
        outbound_label.setObjectName("sectionTitle")
        layout.addWidget(outbound_label)

        self._outbound_table = QTableWidget(0, 4)
        self._outbound_table.setHorizontalHeaderLabels(
            ["주문 ID", "사용자 ID", "상태", "갱신 시각"]
        )
        self._outbound_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._outbound_table.verticalHeader().setVisible(False)
        self._outbound_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._outbound_table.setMaximumHeight(200)
        layout.addWidget(self._outbound_table)

    def refresh(self) -> None:
        # AMR은 Management gRPC 워커가 마지막으로 전달한 상태만 사용
        amrs = self._amr_live
        self._update_amr_cards(amrs)

        try:
            snapshot = self._mgmt.get_logistics_snapshot()
        except grpc.RpcError as exc:
            logger.warning(
                "GetLogisticsSnapshot 실패, 마지막 정상 화면 유지: %s",
                exc,
            )
            return

        tasks = self._sort_tasks(snapshot["tasks"])
        self._update_task_table(tasks)
        self._update_outbound_table(snapshot["orders"])
        self._update_kpis(amrs, tasks)

    def _update_kpis(
        self,
        amrs: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
    ) -> None:
        active_tasks = sum(
            1 for task in tasks if _task_status(task) == "PROC"
        )
        pending_tasks = sum(
            1 for task in tasks if _task_status(task) == "QUE"
        )
        completed_tasks = sum(
            1 for task in tasks if _task_status(task) == "SUCC"
        )
        idle_amrs = sum(
            1 for amr in amrs if int(amr.get("task_state", 0) or 0) == 1
        )

        self._kpi_cards["active_tasks"].update_value(active_tasks)
        self._kpi_cards["pending_tasks"].update_value(pending_tasks)
        self._kpi_cards["completed_tasks"].update_value(completed_tasks)
        self._kpi_cards["idle_amr"].update_value(idle_amrs)

    def _update_amr_cards(self, amrs: list[dict[str, Any]]) -> None:
        existing_ids = set(self._amr_cards.keys())
        seen: set[str] = set()

        for amr in amrs:
            amr_id = str(amr.get("id", ""))
            if not amr_id:
                continue
            seen.add(amr_id)

            card = self._amr_cards.get(amr_id)
            if card is None:
                card = AmrStatusCard(amr_id)
                self._amr_cards[amr_id] = card
                self._amr_row.addWidget(card, stretch=1)
            card.update_from_dict(amr)

        # 사라진 AMR 제거
        for gone_id in existing_ids - seen:
            card = self._amr_cards.pop(gone_id, None)
            if card is not None:
                self._amr_row.removeWidget(card)
                card.deleteLater()

    @staticmethod
    def _sort_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        status_order = {"PROC": 0, "QUE": 1, "FAIL": 2, "SUCC": 3}
        return sorted(
            tasks,
            key=lambda task: status_order.get(_task_status(task), 9),
        )

    def _update_task_table(self, tasks: list[dict[str, Any]]) -> None:
        self._task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            self._task_table.setItem(
                row,
                0,
                QTableWidgetItem(str(task.get("txn_id", ""))),
            )
            self._task_table.setItem(
                row,
                1,
                QTableWidgetItem(str(task.get("task_type", ""))),
            )
            amr_item = QTableWidgetItem(str(task.get("res_id", "")))
            amr_item.setTextAlignment(Qt.AlignCenter)
            self._task_table.setItem(row, 2, amr_item)
            self._task_table.setItem(
                row,
                3,
                QTableWidgetItem(str(task.get("item_id", "") or "-")),
            )
            self._task_table.setItem(
                row,
                4,
                QTableWidgetItem(str(task.get("ord_id", "") or "-")),
            )
            status = _task_status(task)
            status_item = QTableWidgetItem(
                TASK_STATUS_TEXT.get(status, status)
            )
            status_item.setTextAlignment(Qt.AlignCenter)
            self._task_table.setItem(row, 5, status_item)
            self._task_table.setItem(
                row,
                6,
                QTableWidgetItem(str(task.get("req_at", ""))),
            )

    def _update_outbound_table(self, orders: list[dict[str, Any]]) -> None:
        self._outbound_table.setRowCount(len(orders))
        for row, order in enumerate(orders):
            self._outbound_table.setItem(
                row,
                0,
                QTableWidgetItem(str(order.get("ord_id", ""))),
            )
            self._outbound_table.setItem(
                row,
                1,
                QTableWidgetItem(str(order.get("user_id", ""))),
            )
            status = str(order.get("stat", "")).upper()
            status_item = QTableWidgetItem(
                ORDER_STATUS_TEXT.get(status, status)
            )
            status_item.setTextAlignment(Qt.AlignCenter)
            self._outbound_table.setItem(row, 2, status_item)
            self._outbound_table.setItem(
                row,
                3,
                QTableWidgetItem(str(order.get("updated_at", ""))),
            )

    def update_amr_live(self, amr_list: list[dict[str, Any]]) -> None:
        """AMR 실시간 데이터(배터리 등) 수신 — 이후 refresh()에서도 이 데이터를 사용."""
        self._amr_live = amr_list
        self._update_amr_cards(amr_list)



def _task_status(task: dict[str, Any]) -> str:
    return str(task.get("txn_stat", "")).upper()
