"""factory_map 위젯 — 색상/크기/좌표 상수.

Scene 크기, 색상 팔레트, AMR 상태→색상 매핑, 시뮬레이션 좌표 (_POS).
다른 모듈이 `from ._constants import ...` 로 import.

2026-04-27: backend/app/widgets/factory_map.py (1069 LOC) 분할 산출.
"""

from __future__ import annotations

from PyQt5.QtGui import QColor

# ---------------------------------------------------------------------------
# 씬 크기 — real_maplayout.png 실제 해상도에 맞춤 (1150×625)
# ---------------------------------------------------------------------------
SCENE_W = 1150
SCENE_H = 625

# ---------------------------------------------------------------------------
# 색상
# ---------------------------------------------------------------------------
BG_COLOR = "#e8e8e8"  # 전체 배경
ZONE_BG = QColor(160, 180, 210, 140)  # 구역 배경 (반투명 파란)
ZONE_BORDER = QColor(60, 70, 110)  # 구역 파선 테두리
LABEL_BG = "#ffd700"  # 노란 라벨
LABEL_TEXT = "#000000"
EQUIP_BG = "#ffffff"  # 장비 흰색 배경
CONVEYOR_COLOR = "#228b22"  # 컨베이어 초록
RED_BLOCK = "#ff3300"  # 빨간 사각형
MOLD_COLOR = "#b0d4f1"  # 하늘색 몰드
AMR_COLOR = "#e6c619"  # AMR 노란색
CHARGING_BG = QColor(200, 200, 200)
OUTBOUND_GRAD_TOP = "#d0d0d0"
OUTBOUND_GRAD_BOT = "#a0a0a0"
WORKER_COLOR = "#333333"

# AMR 상태 색상
STATUS_COLORS: dict[str, dict[str, str]] = {
    "active": {"dot": "#4ade80", "border": "#22c55e"},
    "idle": {"dot": "#9ca3af", "border": "#6b7280"},
    "warning": {"dot": "#fbbf24", "border": "#f59e0b"},
    "error": {"dot": "#f87171", "border": "#ef4444"},
    "charging": {"dot": "#60a5fa", "border": "#60a5fa"},
}


# ---------------------------------------------------------------------------
# 시뮬레이션 좌표
# ---------------------------------------------------------------------------
# 주요 좌표 상수 (zone 내부 + 통로 웨이포인트)
_POS = {
    # 주조 기계 (이미지 좌측 끝)
    "mold": (100, 465),
    # Casting waiting zone (중앙)
    "cast_wait": (640, 325),
    "cast_pickup": (560, 265),
    # Charging zone (중앙 하단) — AMR 홈
    "amr1_home": (525, 528),
    "amr2_home": (638, 528),
    "amr3_home": (750, 528),
    # Postprocessing zone (좌상단)
    "unload_worker": (225, 105),
    "pp_worker": (325, 105),
    "conv_left": (345, 155),
    "conv_right": (590, 155),
    "conv_wait": (640, 155),
    "conv_pickup": (555, 252),
    # Staging location (좌하단 위 박스)
    "rack": (160, 340),
    "rack_pickup": (268, 340),
    # Putaway waiting zone (좌하단 아래 박스)
    "putaway": (215, 515),
    # Shipping zone (우상단)
    "outbound": (1000, 115),
    # 통로 웨이포인트
    "corridor_top": (450, 238),
    "corridor_right": (760, 238),
    "corridor_left": (185, 238),
    "corridor_mid": (580, 238),
    "corridor_bottom_left": (375, 440),
    "corridor_outbound_r": (875, 238),
    "corridor_outbound_up": (1000, 238),
    "corridor_storage_bot": (375, 440),
    "corridor_charge_exit_1": (525, 440),
    "corridor_charge_exit_2": (638, 440),
    "corridor_charge_exit_3": (750, 440),
}

# 시뮬레이션 파라미터
CASTING_COLOR = "#9333ea"  # 보라색
CASTING_RADIUS = 8
SIM_SPEED = 10  # px per tick (200ms 주기 보정 — 같은 시각 속도 유지)
SIM_TICK_MS = 200  # 200ms = 5Hz (이전 50ms = 20Hz 에서 4배 완화)


def _status_key(raw: str) -> str:
    """외부 status 라벨 → STATUS_COLORS key 정규화."""
    s = (raw or "").lower()
    mapping = {
        "running": "active",
        "active": "active",
        "completed": "active",
        "idle": "idle",
        "waiting": "idle",
        "warning": "warning",
        "maintenance": "warning",
        "error": "error",
        "alarm": "error",
        "charging": "charging",
    }
    return mapping.get(s, "idle")


def _move_toward(
    cx: float, cy: float, tx: float, ty: float, speed: float
) -> tuple[float, float, bool]:
    """(cx,cy)에서 (tx,ty)를 향해 speed만큼 이동. 도착하면 arrived=True."""
    dx, dy = tx - cx, ty - cy
    dist = (dx * dx + dy * dy) ** 0.5
    if dist <= speed:
        return tx, ty, True
    ratio = speed / dist
    return cx + dx * ratio, cy + dy * ratio, False


# ---------------------------------------------------------------------------
# AMCL → scene 좌표 변환
# ---------------------------------------------------------------------------
# Final_map.yaml 기반: origin=[-0.830, -1.314], resolution=0.025, pgm=46×84
# 좌표계: x 오른쪽(+), y 아래쪽(+) — poses.yaml 와 일치
# CHG zone(x=0.06, y=0.095) → 오른쪽 하단 확인 기준으로 도출.
_AMCL_ORIGIN_X: float = -0.830
_AMCL_ORIGIN_Y: float = -1.314
_AMCL_SCALE_X: float = SCENE_W / (46 * 0.025)   # ≈ 1000 px/m
_AMCL_SCALE_Y: float = SCENE_H / (84 * 0.025)   # ≈ 297.6 px/m


def amcl_to_scene(x_m: float, y_m: float) -> tuple[float, float]:
    """AMCL 좌표(m) → scene 픽셀 좌표.

    교정이 필요하면 _AMCL_ORIGIN_* / _AMCL_SCALE_* 상수를 조정.
    """
    sx = _AMCL_SCALE_X * (x_m - _AMCL_ORIGIN_X)
    sy = _AMCL_SCALE_Y * (y_m - _AMCL_ORIGIN_Y)
    return sx, sy


# ---------------------------------------------------------------------------
# PAT / MAT 고정 위치 (픽셀, 이미지 기준)
# ---------------------------------------------------------------------------
# 실제 이미지에서 확인 후 갱신. MAT=주조 arm, PAT=피킹/출하 arm.
PAT_FIXED_POS: tuple[float, float] = (291.0, 393.0)# (700.0, 220.0)
MAT_FIXED_POS: tuple[float, float] = (948.0, 419.0) # (90.0, 460.0)
