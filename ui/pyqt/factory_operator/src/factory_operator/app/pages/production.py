"""생산 모니터링 페이지 — 제어 패널 + 실시간 공정 파라미터 + 냉각 + 차트 2종.

2026-04-27: 사용자 재요청으로 부활. 이전 통합(operations) 으로 옮긴 공정 단계/Item 위치
표는 'operations(실시간 운영 모니터링)' 에 그대로 두고, 본 페이지는 HW 직결 제어 + 시각화
위젯 (이미지 기준) 만 담당한다.

레이아웃 (이미지 그대로):
  ┌─ 제어 패널 (E-STOP + AUTO/MANUAL) ─┬─ 실시간 공정 파라미터 (3 게이지) ─┬─ 냉각 진행 ─┐
  │                                       │ 성형 압력 / 주탕 각도 / 가열 출력 │   원형 %    │
  ├───────────────────────────────────────┴──────────────────────────────┴─────────────┤
  ├─ 용해로 온도 (현재/목표 라인) ────────────┬─ 시간별 생산량/불량 (스택 바) ─────────┤
  └───────────────────────────────────────────┴────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import time
import grpc
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

# 시간별 생산량/용해로 온도/공정 파라미터 갱신 주기 (초)
# - 시간별 차트는 1시간 단위 집계 → 60초 이내 변화 거의 없음. 60초 throttle 로 충분.
# - 게이지(압력/각도/출력) + 냉각 진행의 실데이터 미연동 Mock은 값이 거의 변하지 않으므로
#   동일 throttle 적용. 실데이터 연결 후에도 Management gRPC 계약 유지.
_HOURLY_REFRESH_SEC = 60.0
_GAUGE_REFRESH_SEC = 5.0  # 게이지는 좀 더 자주

from app import mock_data
from app.management_client import ManagementClient
from app.widgets.charts import HourlyProductionChart, TemperatureChart
from app.widgets.gauges import ArcGauge, CircularProgress, ControlPanel

logger = logging.getLogger(__name__)


class ProductionPage(QWidget):
    """제어/게이지/차트 중심 생산 모니터링."""

    def __init__(self, mgmt: ManagementClient) -> None:
        super().__init__()
        self._mgmt = mgmt
        # throttle: 마지막 호출 시각 (monotonic). 0 = 미호출 → 첫 refresh 강제 통과.
        self._last_hourly_at: float = 0.0
        self._last_gauge_at: float = 0.0
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
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
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)

        title = QLabel("생산 모니터링")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        # ── 상단 Row: 제어 + 게이지 + 냉각 ──
        top_row = QHBoxLayout()
        top_row.setSpacing(14)

        self._control_panel = ControlPanel()
        self._control_panel.e_stop.pressed_stop.connect(self._on_emergency_stop)
        self._control_panel.mode_toggle.toggled_changed.connect(self._on_mode_changed)
        top_row.addWidget(self._control_panel, stretch=1)

        gauge_card = QFrame()
        gauge_card.setObjectName("gaugeCard")
        gauge_card.setFrameShape(QFrame.StyledPanel)
        gauge_v = QVBoxLayout(gauge_card)
        gauge_v.setContentsMargins(12, 8, 12, 8)
        gauge_v.setSpacing(4)

        self._gauge_title = QLabel("실시간 공정 파라미터")
        self._gauge_title.setObjectName("sectionTitle")
        gauge_v.addWidget(self._gauge_title)

        gauge_row = QHBoxLayout()
        gauge_row.setSpacing(4)
        self._gauge_pressure = ArcGauge(
            title="성형 압력",
            unit="bar",
            minimum=0,
            maximum=150,
            warn_ratio=0.7,
            danger_ratio=0.9,
        )
        self._gauge_angle = ArcGauge(
            title="주탕 각도",
            unit="deg",
            minimum=0,
            maximum=90,
            warn_ratio=0.75,
            danger_ratio=0.95,
        )
        self._gauge_power = ArcGauge(
            title="가열 출력",
            unit="%",
            minimum=0,
            maximum=100,
            warn_ratio=0.85,
            danger_ratio=0.95,
        )
        gauge_row.addWidget(self._gauge_pressure)
        gauge_row.addWidget(self._gauge_angle)
        gauge_row.addWidget(self._gauge_power)
        gauge_v.addLayout(gauge_row)
        top_row.addWidget(gauge_card, stretch=3)

        cooling_card = QFrame()
        cooling_card.setObjectName("gaugeCard")
        cooling_card.setFrameShape(QFrame.StyledPanel)
        cooling_v = QVBoxLayout(cooling_card)
        cooling_v.setContentsMargins(12, 12, 12, 12)
        self._cooling_source = QLabel("")
        self._cooling_source.setAlignment(Qt.AlignCenter)
        cooling_v.addWidget(self._cooling_source)
        self._cooling = CircularProgress(title="냉각 진행", subtitle="-", unit="%")
        cooling_v.addWidget(self._cooling)
        top_row.addWidget(cooling_card, stretch=1)

        layout.addLayout(top_row)

        # ── 하단 Row: 용해로 온도 + 시간별 생산량 ──
        chart_row = QHBoxLayout()
        chart_row.setSpacing(14)
        self._temp_chart = TemperatureChart()
        self._temp_chart.setMinimumHeight(220)
        chart_row.addWidget(self._temp_chart, stretch=1)

        self._hourly_chart = HourlyProductionChart()
        self._hourly_chart.setMinimumHeight(220)
        chart_row.addWidget(self._hourly_chart, stretch=1)
        layout.addLayout(chart_row, stretch=1)

        layout.addStretch(1)

    # ------------------------------------------------------------------
    # Refresh — backend 데이터로 위젯 갱신
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """주기 갱신. 게이지 5초 / 시간별 차트 60초 throttle.

        MainWindow._timer가 8초마다 호출하므로 telemetry snapshot은 갱신 대상이
        있는 경우에만 요청.
        """
        now = time.monotonic()
        refresh_live = now - self._last_gauge_at >= _GAUGE_REFRESH_SEC
        refresh_charts = now - self._last_hourly_at >= _HOURLY_REFRESH_SEC
        if not refresh_live and not refresh_charts:
            return

        try:
            snapshot = self._mgmt.get_production_telemetry_snapshot(hours=24)
        except grpc.RpcError as exc:
            logger.warning(
                "GetProductionTelemetrySnapshot 실패, 마지막 정상 화면 유지: %s",
                exc,
            )
            return

        if refresh_live:
            self._last_gauge_at = now
            live = snapshot["live"]
            source_available = bool(live["source_available"])
            params = (
                live["value"]
                if source_available
                else dict(mock_data.LIVE_PARAMETERS)
            )
            self._set_live_source_available(source_available)
            self._gauge_pressure.set_value(params.get("mold_pressure", 0))
            self._gauge_angle.set_value(params.get("pour_angle", 0))
            self._gauge_power.set_value(params.get("furnace_heating_power", 0))

            cooling_progress = params.get("cooling_progress", 0)
            current_t = params.get("cooling_current_temp", 0)
            target_t = params.get("cooling_target_temp", 25)
            remaining = params.get("cooling_remaining_min", 0)
            self._cooling.set_value(cooling_progress)
            self._cooling.set_subtitle(
                f"{current_t:.0f}°C → {target_t:.0f}°C, {remaining:g}분 남음"
            )

        if refresh_charts:
            self._last_hourly_at = now
            temperature = snapshot["temperature"]
            temperature_source_available = bool(
                temperature["source_available"]
            )
            temperature_rows = (
                temperature["entries"]
                if temperature_source_available
                else mock_data.TEMPERATURE_HISTORY
            )
            self._temp_chart.update_data(temperature_rows)
            self._temp_chart.set_source_available(
                temperature_source_available
            )

            hourly = snapshot["hourly"]
            hourly_source_available = bool(hourly["source_available"])
            hourly_rows = (
                hourly["entries"]
                if hourly_source_available
                else mock_data.HOURLY_PRODUCTION
            )
            self._hourly_chart.update_data(hourly_rows)
            self._hourly_chart.set_source_available(
                hourly_source_available
            )

    def _set_live_source_available(self, available: bool) -> None:
        suffix = "" if available else " (예시 데이터, 실데이터 미연동)"
        self._gauge_title.setText(f"실시간 공정 파라미터{suffix}")
        self._cooling_source.setText(
            "" if available else "예시 데이터, 실데이터 미연동"
        )

    # ------------------------------------------------------------------
    # 제어 이벤트
    # ------------------------------------------------------------------
    def _on_emergency_stop(self) -> None:
        QMessageBox.warning(
            self,
            "비상 정지 (E-STOP)",
            "비상정지 버튼이 활성화되었습니다.\n\n"
            "현재는 화면 경고만 표시하며 정지 신호는 전송하지 않습니다.\n"
            "HW command RPC 연결 전에는 현장 정지 절차를 사용하세요.",
            QMessageBox.Ok,
        )
        # TODO: HW 연동 시 Management 비상 정지 command RPC 추가

    def _on_mode_changed(self, auto: bool) -> None:
        # TODO: HW 연동 시 Management 운전 모드 command RPC 추가
        _ = "AUTO" if auto else "MANUAL"
