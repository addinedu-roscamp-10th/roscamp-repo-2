"""비전 카메라 피드 카드 — web VisionCameraFeed 의 PyQt 버전.

이미지가 도착하지 않은 상태에선 placeholder (대시 원 + CAM-001 + casting_id) 를 표시.
나중에 카메라/검사 이미지 도착 시 ``set_image(pixmap)`` 호출로 채워 넣는다.

표시 항목 (web 과 1:1):
- 상단 우측: PASS / FAIL 배지 (선택된 검사가 있을 때만)
- 하단 우측: 타임스탬프 (HH:MM:SS)
- 중앙: 이미지 또는 placeholder (item_id: <value>)
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QFrame, QGridLayout, QLabel, QSizePolicy


class VisionFeedCard(QFrame):
    """카메라 피드 시뮬 카드. 16:9 비율 검정 배경 + 4 코너 오버레이."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("visionFeedCard")
        # 검정 배경 + 둥근 모서리 + 약한 테두리.
        self.setStyleSheet(
            "#visionFeedCard { background:#0a0a0a; border:1px solid #1f2937; "
            "border-radius:12px; }"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(220)

        grid = QGridLayout(self)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setSpacing(0)

        # 상단 우측 — PASS/FAIL 배지 (선택된 검사가 있을 때만 노출).
        self._badge = QLabel("")
        self._badge.setObjectName("visionBadge")
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setMinimumWidth(72)
        self._badge.setStyleSheet(self._badge_style(None))
        self._badge.hide()
        grid.addWidget(self._badge, 0, 2, alignment=Qt.AlignRight | Qt.AlignTop)

        # 중앙 — 이미지 / placeholder.
        self._image = QLabel()
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setMinimumSize(160, 120)
        self._image.setStyleSheet(
            "color:#16a34a; font-family:'Menlo','Monaco',monospace; font-size:11px; "
            "letter-spacing:1px;"
        )
        self._image.setText(self._placeholder_text(None))
        grid.addWidget(self._image, 1, 0, 1, 3, alignment=Qt.AlignCenter)

        # 하단 우측 — 타임스탬프.
        self._timestamp = QLabel("--:--:--")
        self._timestamp.setStyleSheet(
            "color:#9ca3af; font-family:'Menlo','Monaco',monospace; font-size:11px; "
            "background:rgba(0,0,0,0.7); padding:3px 8px; border-radius:6px; "
            "border:1px solid #374151;"
        )
        grid.addWidget(self._timestamp, 2, 2, alignment=Qt.AlignRight | Qt.AlignBottom)

        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)

        self._last_pixmap: QPixmap | None = None

    # ---- 외부 API --------------------------------------------------------

    def update_data(self, inspection: dict[str, Any] | None) -> None:
        """선택된 검사 결과로 PASS/FAIL · 타임스탬프 · item_id 라벨 갱신.

        검사 선택이 없으면 PASS/FAIL 배지는 숨김 (검사 진행 중 카메라 아님).
        """
        result = (inspection or {}).get("result") if inspection else None
        is_pass = result in ("OK", "pass")
        is_fail = result in ("NG", "fail")

        if is_pass:
            self._badge.setText("PASS")
            self._badge.setStyleSheet(self._badge_style(True))
            self._badge.show()
        elif is_fail:
            self._badge.setText("FAIL")
            self._badge.setStyleSheet(self._badge_style(False))
            self._badge.show()
        else:
            self._badge.hide()

        at = (inspection or {}).get("inspected_at") or (inspection or {}).get("inspectedAt") or ""
        self._timestamp.setText(self._format_time(at))

        # 사용자 요청: 중앙 라벨에 item_id 표시. INSPECTIONS row 는 casting_id 값을 사용.
        item_id = (
            (inspection or {}).get("item_id")
            or (inspection or {}).get("casting_id")
            or (inspection or {}).get("castingId")
            or "---"
        )
        if self._last_pixmap is None:
            self._image.setText(self._placeholder_text(item_id))

    def set_image(self, pixmap: QPixmap | None) -> None:
        """카메라 프레임/검사 이미지가 도착했을 때 호출."""
        self._last_pixmap = pixmap
        if pixmap is None or pixmap.isNull():
            self._image.setText(self._placeholder_text(None))
            return
        scaled = pixmap.scaled(
            self._image.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._image.setPixmap(scaled)

    # ---- 내부 헬퍼 -------------------------------------------------------

    @staticmethod
    def _badge_style(state: bool | None) -> str:
        if state is True:
            return (
                "color:#ffffff; background:#16a34a; padding:4px 10px; "
                "border-radius:6px; font-weight:700; font-size:13px;"
            )
        if state is False:
            return (
                "color:#ffffff; background:#dc2626; padding:4px 10px; "
                "border-radius:6px; font-weight:700; font-size:13px;"
            )
        return (
            "color:#6b7280; background:rgba(255,255,255,0.05); padding:4px 10px; "
            "border-radius:6px; font-weight:700; font-size:13px; "
            "border:1px solid #374151;"
        )

    @staticmethod
    def _placeholder_text(item_id: str | None) -> str:
        """RichText — NO IMAGE 워터마크(반투명 회색) + item_id 라벨."""
        iid = item_id or "---"
        return (
            '<div align="center">'
            '<span style="font-size:34px; font-weight:700; color:#4b5563; '
            'letter-spacing:8px;">NO IMAGE</span>'
            '<br><br>'
            f'<span style="font-size:11px; color:#16a34a; '
            f'font-family:Menlo,Monaco,monospace;">item_id: {iid}</span>'
            '</div>'
        )

    @staticmethod
    def _format_time(at_iso: str) -> str:
        # "2026-03-30T09:31:00" → "09:31:00", "03/30 09:31" → 그대로.
        if not at_iso:
            return "--:--:--"
        if "T" in at_iso:
            tail = at_iso.split("T", 1)[1]
            return tail[:8] if len(tail) >= 5 else tail
        return at_iso
