"""factory_map 위젯 — FactoryMapScene (이미지 배경 + AMR 마커 보간 애니메이션).

real_maplayout.png 를 배경으로 깔고, _SimController 가 AMR/주물 시뮬레이션을 오버레이.
update_equipment(amrs) → 실제 API AMR 마커 갱신 + _tick_animation(100ms) 이 보간 이동.
enable_sim=True 면 _SimController 시작 (메인 맵에서만 활성).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PyQt5.QtCore import QPointF, Qt, QTimer
from PyQt5.QtGui import QBrush, QColor, QFont, QPen, QPixmap
from PyQt5.QtWidgets import (
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
)

from ._constants import (
    SCENE_H,
    SCENE_W,
    STATUS_COLORS,
    _status_key,
)
from .sim import _SimController

_ASSETS = Path(__file__).parent.parent.parent / "assets"


class FactoryMapScene(QGraphicsScene):
    """공장 레이아웃 씬 — 이미지 배경 + AMR 마커 보간 애니메이션."""

    ANIMATION_INTERVAL_MS = 100
    ANIMATION_STEP = 0.12

    def __init__(self, enable_sim: bool = True) -> None:
        super().__init__()
        self.setSceneRect(0, 0, SCENE_W, SCENE_H)
        self._amr_state: dict[str, dict[str, Any]] = {}

        self._draw_background()

        self._anim_timer = QTimer()
        self._anim_timer.setInterval(self.ANIMATION_INTERVAL_MS)
        self._anim_timer.timeout.connect(self._tick_animation)
        self._anim_timer.start()

        self._sim: _SimController | None = None
        if enable_sim:
            self._sim = _SimController(self)
            self._sim.start()

    def _draw_background(self) -> None:
        img_path = _ASSETS / "real_maplayout.png"
        pixmap = QPixmap(str(img_path))
        if pixmap.isNull():
            self.setBackgroundBrush(QBrush(QColor("#e8e8e8")))
            return
        item = self.addPixmap(pixmap)
        item.setZValue(0)

    # ------------------------------------------------------------------
    # 장비 갱신 (외부 호출 — 실제 API 데이터)
    # ------------------------------------------------------------------
    def update_equipment(self, equipment: list[dict[str, Any]]) -> None:
        # _SimController 가 AMR 을 소유하므로 API 마커와 충돌하지 않도록 클리어만.
        self._clear_equipment_amr_markers()

    def _update_amr_markers(self, amrs: list[dict[str, Any]]) -> None:
        seen: set[str] = set()
        charge_x_start, charge_y = 497, 471
        charge_spacing = 98

        for idx, amr in enumerate(amrs):
            amr_id = str(amr.get("id", ""))
            if not amr_id:
                continue
            seen.add(amr_id)

            status = str(amr.get("status", ""))
            status_key = _status_key(status)
            color_info = STATUS_COLORS.get(status_key, STATUS_COLORS["idle"])
            color = color_info["dot"]

            pos_x_m = amr.get("pos_x")
            pos_y_m = amr.get("pos_y")
            if pos_x_m is not None and pos_y_m is not None:
                target_x = 50 + (float(pos_x_m) / 32.0) * (SCENE_W - 100)
                target_y = 50 + (float(pos_y_m) / 12.0) * (SCENE_H - 100)
            else:
                target_x = charge_x_start + idx * charge_spacing
                target_y = charge_y

            state = self._amr_state.get(amr_id)
            if state is None:
                marker = QGraphicsRectItem(-20, -12, 40, 24)
                marker.setBrush(QBrush(QColor(color)))
                marker.setPen(QPen(QColor(color_info["border"]), 2))
                marker.setZValue(50)
                self.addItem(marker)

                label = QGraphicsSimpleTextItem(amr_id, marker)
                label.setFont(QFont("Sans", 7, QFont.Bold))
                label.setBrush(QBrush(QColor("#111111")))
                lr = label.boundingRect()
                label.setPos(-lr.width() / 2, -lr.height() / 2)

                trail = self.addLine(
                    target_x, target_y, target_x, target_y,
                    QPen(QColor(color), 2, Qt.DashLine),
                )
                trail.setZValue(40)

                marker.setPos(target_x, target_y)
                self._amr_state[amr_id] = {
                    "marker": marker,
                    "label": label,
                    "trail": trail,
                    "current": QPointF(target_x, target_y),
                    "target": QPointF(target_x, target_y),
                    "color": color,
                    "data": amr,
                }
            else:
                state["target"] = QPointF(target_x, target_y)
                state["color"] = color
                state["data"] = amr
                state["marker"].setBrush(QBrush(QColor(color)))
                tp = state["trail"].pen()
                tp.setColor(QColor(color))
                state["trail"].setPen(tp)

            st = self._amr_state[amr_id]
            battery = amr.get("battery")
            tooltip = f"{amr_id}\nstatus: {status}"
            if battery is not None:
                tooltip += f"\nbattery: {battery}%"
            st["marker"].setToolTip(tooltip)

        for amr_id in list(self._amr_state.keys()):
            if amr_id not in seen:
                st = self._amr_state.pop(amr_id)
                self.removeItem(st["marker"])
                self.removeItem(st["trail"])

    def _clear_equipment_amr_markers(self) -> None:
        for st in self._amr_state.values():
            self.removeItem(st["marker"])
            self.removeItem(st["trail"])
        self._amr_state.clear()

    def _tick_animation(self) -> None:
        for state in self._amr_state.values():
            current: QPointF = state["current"]
            target: QPointF = state["target"]
            dx = target.x() - current.x()
            dy = target.y() - current.y()
            if dx * dx + dy * dy < 0.25:
                continue
            new_x = current.x() + dx * self.ANIMATION_STEP
            new_y = current.y() + dy * self.ANIMATION_STEP
            state["current"] = QPointF(new_x, new_y)
            state["marker"].setPos(new_x, new_y)
            state["trail"].setLine(new_x - dx, new_y - dy, new_x, new_y)
