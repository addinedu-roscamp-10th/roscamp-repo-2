"""품질 검사 모니터링 페이지 v0.5.

구성:
  1. KPI 4개 (조건부 색상 적용)
  2. 분류 다이얼 + TOP3 불량 배지 + 검사 기준 참조 패널
  3. 차트 3개 (불량률 추이 / 불량 분포 / 생산량 vs 불량)
  4. 검사 이력 테이블
"""

from __future__ import annotations

import logging
from typing import Any

import grpc
from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QColor, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import mock_data
from app.management_client import ManagementClient
from app.pages.dashboard import KpiCard
from app.widgets.charts import (
    DefectRateChart,
    DefectTypeDistChart,
    ProductionVsDefectsChart,
)
from app.widgets.defect_panels import InspectionStandardsPanel, TopDefectsPanel
# 2026-05-14: SorterCard 제거 — VisionFeedCard (web VisionCameraFeed 와 동등) 로 교체.
from app.widgets.vision_feed import VisionFeedCard

logger = logging.getLogger(__name__)


class QualityPage(QWidget):
    def __init__(self, mgmt: ManagementClient) -> None:
        super().__init__()
        self._mgmt = mgmt
        self._kpis: dict[str, KpiCard] = {}
        self._build_ui()
        self.refresh()

    def shutdown(self) -> None:
        """앱 종료 시 main_window 에서 호출."""
        return None

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

        title = QLabel("품질 검사")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        # ===== KPI 4 카드 =====
        kpi_grid = QGridLayout()
        kpi_grid.setSpacing(14)
        metrics = [
            ("total", "검사 수", "건"),
            ("ok", "합격", "건"),
            ("ng", "불합격", "건"),
            ("rate", "불량률", "%"),
        ]
        for col, (key, label, unit) in enumerate(metrics):
            card = KpiCard(label, unit=unit)
            self._kpis[key] = card
            kpi_grid.addWidget(card, 0, col)
        layout.addLayout(kpi_grid)

        # ===== 분류 다이얼 + TOP3 + 기준 =====
        top_row = QHBoxLayout()
        top_row.setSpacing(14)

        self._vision_feed = VisionFeedCard()
        top_row.addWidget(self._vision_feed, stretch=2)

        self._top_defects = TopDefectsPanel()
        top_row.addWidget(self._top_defects, stretch=2)

        self._standards = InspectionStandardsPanel()
        top_row.addWidget(self._standards, stretch=4)

        layout.addLayout(top_row, stretch=1)

        # ===== 차트 3개 =====
        chart_row = QHBoxLayout()
        chart_row.setSpacing(14)

        self._rate_chart = DefectRateChart()
        self._rate_chart.setMinimumHeight(220)
        chart_row.addWidget(self._rate_chart, stretch=2)

        self._pie_chart = DefectTypeDistChart()
        self._pie_chart.setMinimumHeight(220)
        chart_row.addWidget(self._pie_chart, stretch=1)

        self._vs_chart = ProductionVsDefectsChart()
        self._vs_chart.setMinimumHeight(220)
        chart_row.addWidget(self._vs_chart, stretch=2)

        layout.addLayout(chart_row, stretch=1)

        # ===== 검사 이력 테이블 =====
        section = QLabel("최근 검사 이력")
        section.setObjectName("sectionTitle")
        layout.addWidget(section)

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(
            ["이미지", "검사 시각", "제품", "결과", "불량 유형", "담당자", "비고"]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setIconSize(QSize(96, 54))
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        # 2026-05-14: 최근 검사 이력 풀 노출 — 페이지 외곽 스크롤이 처리.
        self._table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 행 클릭 → 비전 피드 동기화 + 선택 색상 자동 적용.
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.cellClicked.connect(self._on_inspection_row_clicked)
        self._inspections_cache: list[dict] = []
        # 2026-05-15: 사용자 선택 보존. 주기 refresh 가 vision feed 를 덮어쓰지 않도록.
        self._selected_inspection_id: str | None = None
        self._loaded_inference_id: int | None = None
        self._image_bytes_cache: dict[int, bytes] = {}
        layout.addWidget(self._table)

        # ===== 검사 진행 중 (PROC) — 결과 입력 (Gap 5, 2026-04-27) =====
        proc_label = QLabel("검사 진행 중 (결과 입력 대기)")
        proc_label.setObjectName("sectionTitle")
        layout.addWidget(proc_label)

        self._proc_table = QTableWidget(0, 6)
        self._proc_table.setHorizontalHeaderLabels(
            ["검사 ID", "제품 ID", "검사 결과", "시작 시각", "양품", "불량"]
        )
        self._proc_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._proc_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._proc_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self._proc_table.verticalHeader().setVisible(False)
        self._proc_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._proc_table.setMaximumHeight(180)
        layout.addWidget(self._proc_table)

    def refresh(self) -> None:
        try:
            snapshot = self._mgmt.get_quality_snapshot(
                hours=24,
                inspection_limit=200,
            )
        except grpc.RpcError as exc:
            logger.warning("GetQualitySnapshot 실패, 마지막 정상 화면 유지: %s", exc)
            return

        stats = snapshot["stats"]
        total = stats.get("total", 0)
        ok = stats.get("ok", 0)
        ng = stats.get("ng", 0)
        rate = float(stats.get("defect_rate", 0))
        self._kpis["total"].update_value(total)
        self._kpis["ok"].update_value(ok)
        self._kpis["ng"].update_value(ng)
        self._kpis["rate"].update_value(f"{rate:.1f}")
        self._colorize_rate(rate)

        # 2026-05-14: 분류 다이얼 → 비전 카메라 피드. 최신 검사 1건을 web 과 동일하게 표시.
        # 2026-05-15: 사용자가 행을 선택한 상태면 그 row 유지, 아니면 latest (INSPECTIONS[0]).
        inspections = snapshot["inspections"]
        target = None
        if self._selected_inspection_id:
            target = next(
                (i for i in inspections if i.get("id") == self._selected_inspection_id),
                None,
            )
        if target is None:
            target = inspections[0] if inspections else None
        self._vision_feed.update_data(target)
        self._load_inspection_image(target)

        # TOP3 + 기준 + 차트
        defect_section = snapshot["defect_types"]
        defect_source_available = bool(defect_section["source_available"])
        defects = (
            defect_section["entries"]
            if defect_source_available
            else mock_data.DEFECT_TYPE_DIST
        )
        self._top_defects.update_data(defects)
        self._top_defects.set_source_available(defect_source_available)
        self._pie_chart.update_data(defects)
        self._pie_chart.set_source_available(defect_source_available)

        standards_section = snapshot["standards"]
        standards_source_available = bool(standards_section["source_available"])
        standards = (
            standards_section["entries"]
            if standards_source_available
            else mock_data.INSPECTION_STANDARDS
        )
        self._standards.update_data(standards)
        self._standards.set_source_available(standards_source_available)

        production_section = snapshot["production_vs_defects"]
        production_source_available = bool(production_section["source_available"])
        production_vs_defects = (
            production_section["entries"]
            if production_source_available
            else mock_data.PRODUCTION_VS_DEFECTS
        )
        self._vs_chart.update_data(production_vs_defects)
        self._vs_chart.set_source_available(production_source_available)

        self._rate_chart.update_data(snapshot["trend"])

        # 검사 이력 — 최근 10건만.
        recent_inspections = inspections[:10]
        self._inspections_cache = recent_inspections
        self._table.setRowCount(len(recent_inspections))
        for row, item in enumerate(recent_inspections):
            result = str(item.get("result", ""))
            inference_id = int(item.get("inference_id") or 0)
            image_bytes = self._get_inspection_image_bytes(inference_id)
            image_item = QTableWidgetItem()
            image_item.setTextAlignment(Qt.AlignCenter)
            if image_bytes:
                pixmap = QPixmap()
                if pixmap.loadFromData(image_bytes) and not pixmap.isNull():
                    image_item.setIcon(QIcon(pixmap))
            if image_item.icon().isNull():
                image_item.setText("—")
            self._table.setItem(row, 0, image_item)

            self._table.setItem(row, 1, QTableWidgetItem(str(item.get("inspected_at", ""))))
            self._table.setItem(row, 2, QTableWidgetItem(str(item.get("product", ""))))

            result_item = QTableWidgetItem(result)
            result_item.setTextAlignment(Qt.AlignCenter)
            if result == "NG":
                result_item.setForeground(QColor("#dc2626"))
            elif result == "OK":
                result_item.setForeground(QColor("#059669"))
            self._table.setItem(row, 3, result_item)
            self._table.setItem(row, 4, QTableWidgetItem(str(item.get("defect_type", ""))))
            self._table.setItem(row, 5, QTableWidgetItem(str(item.get("inspector", ""))))
            self._table.setItem(row, 6, QTableWidgetItem(str(item.get("note", ""))))

        # 모든 row 가 한 번에 보이도록 테이블 높이 동적 조정 (내부 스크롤 사용 안 함).
        self._table.resizeRowsToContents()
        total_h = self._table.horizontalHeader().height() + 4
        for r in range(self._table.rowCount()):
            self._table.setRowHeight(r, max(62, self._table.rowHeight(r)))
            total_h += self._table.rowHeight(r)
        self._table.setFixedHeight(max(80, total_h))

        # ===== 검사 진행 중 (insp_task_txn.txn_stat=PROC) — Gap 5 =====
        self._refresh_proc_table(inspections)

    def _on_inspection_row_clicked(self, row: int, _col: int) -> None:
        """최근 검사 이력 행 클릭 → 비전 피드에 해당 검사 표시 + 이미지 fetch."""
        if 0 <= row < len(self._inspections_cache):
            insp = self._inspections_cache[row]
            # 2026-05-15: 선택 보존 — 주기 refresh 가 latest 로 덮어쓰지 못하게.
            self._selected_inspection_id = insp.get("id") or None
            self._vision_feed.update_data(insp)
            self._load_inspection_image(insp)

    def _load_inspection_image(
        self,
        inspection: dict[str, Any] | None,
    ) -> None:
        """선택한 추론 결과 이미지를 Management gRPC로 조회."""
        inference_id = int((inspection or {}).get("inference_id") or 0)
        if inference_id <= 0:
            self._loaded_inference_id = None
            self._vision_feed.set_image(None)
            return
        if inference_id == self._loaded_inference_id:
            return
        self._loaded_inference_id = None

        image_bytes = self._get_inspection_image_bytes(inference_id)
        if image_bytes is None:
            self._vision_feed.set_image(None)
            return

        if self._vision_feed.set_image_bytes(image_bytes):
            self._loaded_inference_id = inference_id

    def _get_inspection_image_bytes(self, inference_id: int) -> bytes | None:
        """검사 결과 이미지를 한 번만 조회해 피드와 표 썸네일이 공유."""
        if inference_id <= 0:
            return None
        cached = self._image_bytes_cache.get(inference_id)
        if cached is not None:
            return cached

        try:
            image = self._mgmt.get_inspection_image(
                inference_id,
                kind="result",
            )
        except grpc.RpcError as exc:
            logger.warning(
                "GetInspectionImage 실패 inference_id=%s: %s",
                inference_id,
                exc,
            )
            return None

        image_bytes = bytes(image.get("image_bytes") or b"")
        pixmap = QPixmap()
        if not image_bytes or not pixmap.loadFromData(image_bytes) or pixmap.isNull():
            return None
        self._image_bytes_cache[inference_id] = image_bytes
        return image_bytes

    def _refresh_proc_table(self, rows: list[dict[str, Any]]) -> None:
        """진행 중 검사 row 만 골라 GP/DP 버튼 행으로 표시."""
        proc_rows = [r for r in rows if str(r.get("txn_stat", "")).upper() == "PROC"]
        # 최신 우선 정렬
        proc_rows.sort(key=lambda r: r.get("start_at") or r.get("req_at") or "", reverse=True)

        self._proc_table.setRowCount(len(proc_rows))
        for row, r in enumerate(proc_rows):
            txn_id = int(r.get("txn_id", 0))
            cells = [
                str(txn_id),
                str(r.get("item_id", "")),
                str(r.get("res_id", "") or ""),
                str(r.get("start_at", r.get("req_at", "")))[:19],
            ]
            for col, val in enumerate(cells):
                qi = QTableWidgetItem(val)
                qi.setTextAlignment(Qt.AlignCenter)
                self._proc_table.setItem(row, col, qi)

            gp_btn = QPushButton("✅ 양품")
            gp_btn.setToolTip("양품으로 처리 (검사 완료 + 제품의 불량 여부 = 아니오)")
            gp_btn.clicked.connect(lambda _c, t=txn_id: self._complete_inspection(t, True))
            self._proc_table.setCellWidget(row, 4, gp_btn)

            dp_btn = QPushButton("⚠ 불량")
            dp_btn.setToolTip("불량으로 처리 (검사 완료 + 제품의 불량 여부 = 예)")
            dp_btn.clicked.connect(lambda _c, t=txn_id: self._complete_inspection(t, False))
            self._proc_table.setCellWidget(row, 5, dp_btn)

    def _complete_inspection(self, txn_id: int, result: bool) -> None:
        """양품/불량 버튼에서 검사 완료 RPC 호출."""
        verdict = "양품" if result else "불량"
        confirm = QMessageBox.question(
            self,
            "검사 결과 확정",
            f"검사 #{txn_id} 를 {verdict} 으로 처리하시겠습니까?\n\n"
            "확정 시 검사가 완료 처리되고 제품의 불량 여부가 갱신됩니다.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            rsp = self._mgmt.complete_inspection(txn_id, result)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "검사 완료 실패", f"검사 #{txn_id}\n{exc}")
            return

        QMessageBox.information(
            self,
            "검사 완료",
            f"검사 #{txn_id}\n"
            f"  진행 상태: {rsp.get('txn_stat')}\n"
            f"  결과: {verdict}\n"
            f"  완료 시각: {rsp.get('end_at')}\n\n"
            "→ 주문 관리 페이지에서 다음 단계로 진행하세요.",
        )
        self.refresh()

    def _colorize_rate(self, rate: float) -> None:
        """불량률에 따른 KPI 카드 색상 (기준: 2% 미만 녹색, 5% 미만 주황, 초과 빨강)."""
        card = self._kpis["rate"]
        if rate < 2.0:
            color = "#10b981"
        elif rate < 5.0:
            color = "#f59e0b"
        else:
            color = "#ef4444"
        card.setStyleSheet(
            f"#kpiCard {{ border: 2px solid {color}; }}#kpiValue {{ color: {color}; }}"
        )
