"""생산현황 페이지 — 패턴 등록 + 생산 계획(우선순위) 병합 화면.

QSplitter(Qt.Vertical) 로 PatternControlPage 와 SchedulePage 를 위/아래로 배치한다.
두 페이지 모두 자체 좌/우 2열 풀폭 레이아웃을 사용하므로 세로 분할이 가로 분할보다
가독성이 안전하다. 핸들 드래그로 사용자가 비율을 조정할 수 있다.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QSplitter, QVBoxLayout, QWidget

from app.api_client import ApiClient
from app.pages.pattern_control import PatternControlPage
from app.pages.schedule import SchedulePage


class ProductionStatusPage(QWidget):
    """생산현황 — 패턴 등록(상단) + 우선순위 계산(하단) 통합 화면."""

    def __init__(self, api: ApiClient) -> None:
        super().__init__()
        self._api = api
        self._pattern_control = PatternControlPage(api)
        self._schedule = SchedulePage(api)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Vertical, self)
        splitter.setObjectName("productionStatusSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        splitter.addWidget(self._pattern_control)
        splitter.addWidget(self._schedule)
        # 상단(패턴 등록) 이 등록 폼/리스트 양쪽을 모두 보여야 하므로 약간 더 넓게.
        splitter.setStretchFactor(0, 6)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([600, 400])
        outer.addWidget(splitter)

    # main_window 의 _on_nav_changed / _refresh_current_page 가 호출.
    def refresh(self) -> None:
        if hasattr(self._pattern_control, "refresh"):
            self._pattern_control.refresh()
        if hasattr(self._schedule, "refresh"):
            self._schedule.refresh()
