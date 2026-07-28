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
    "mold": (971, 422),
    # Casting waiting zone f(중앙)
    "cast_wait": (822, 313),
    "cast_pickup": (560, 265),
    # Charging zone (중앙 하단) — AMR 홈
    "amr1_home": (611, 507),
    "amr2_home": (677, 506),
    "amr3_home": (737, 507),
    # Postprocessing zone (좌상단)
    "unload_worker": (181, 133),
    "pp_worker": (325, 105),
    "conv_left": (345, 155),
    "conv_right": (590, 155),
    "conv_wait": (640, 155),
    "conv_pickup": (589, 121),
    # Staging location (좌하단 위 박스)
    "rack": (160, 340),
    "rack_pickup": (428, 349),
    # Putaway waiting zone (좌하단 아래 박스)
    "putaway": (427, 422),
    # Shipping zone (우상단)
    "outbound": (945, 127),
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

# ---------------------------------------------------------------------------
# TAT(AMR) 충전존 홈 위치 (인덱스 0=TAT1, 1=TAT2, 2=TAT3)
# ---------------------------------------------------------------------------
AMR_HOME_POSITIONS: list[tuple[float, float]] = [
    _POS["amr1_home"],
    _POS["amr2_home"],
    _POS["amr3_home"],
]

# TAT ID 별 홈 위치 매핑 (Management Service 반환 ID 모두 포함)
TAT_HOME_MAP: dict[str, tuple[float, float]] = {
    "TAT1": _POS["amr1_home"],
    "TAT2": _POS["amr2_home"],
    "TAT3": _POS["amr3_home"],
    "AMR-001": _POS["amr1_home"],
    "AMR-002": _POS["amr2_home"],
    "AMR-003": _POS["amr3_home"],
    "amr1": _POS["amr1_home"],
    "amr2": _POS["amr2_home"],
    "amr3": _POS["amr3_home"],
}

# ---------------------------------------------------------------------------
# Management Service location 문자열 → Scene 좌표
#
# ★ 실제 location 값 확인 방법 (Management Service 서버에서):
#     grep -r "location" management_service/app/amr_battery.py
#     또는 TAT RPi 코드에서 어떤 문자열을 set_location()에 넘기는지 확인
#
# ★ 이 딕셔너리 사용법:
#   1. Management Service 가 robot.location 으로 반환하는 문자열을 키로 넣는다.
#   2. 값은 _POS["..."] 중 해당 zone 의 좌표를 고른다.
#   3. 여러 별칭(동의어)이 있으면 같은 값으로 여러 키를 추가한다.
#   4. 모르는 location 이 있으면 TAT 가 충전존 home 으로 fallback 되므로 무방.
#
# pick_pixel.py 로 좌표 확인:
#   cd ui/pyqt/factory_operator && .venv/bin/python pick_pixel.py
# ---------------------------------------------------------------------------
LOCATION_TO_SCENE: dict[str, tuple[float, float]] = {
    # ── 충전존 (기본값 / AMR home) ─────────────────────────────────────────
    # Management Service 에서 아직 위치를 안 보내면 "-" 가 기본값.
    "-":             _POS["amr2_home"],   # 기본 fallback — 바꾸지 말 것
    "charging":      _POS["amr2_home"],   # ← 실제 문자열로 교체
    "CHG":           _POS["amr2_home"],   # ← 실제 문자열로 교체
    "CH":            _POS["amr2_home"],   # ← 실제 문자열로 교체

    # ── 주조존 (Casting) ──────────────────────────────────────────────────
    "casting":       _POS["cast_wait"],   # ← 실제 문자열로 교체
    "CS":            _POS["cast_wait"],   # ← 실제 문자열로 교체
    "CT":            _POS["cast_wait"],   # ← 실제 문자열로 교체

    # ── 후처리존 (PostProcessing) ─────────────────────────────────────────
    "postprocessing": _POS["unload_worker"],  # ← 실제 문자열로 교체
    "PP":            _POS["unload_worker"],   # ← 실제 문자열로 교체

    # ── 보관존 (Storage / Putaway) ────────────────────────────────────────
    "storage":       _POS["putaway"],     # ← 실제 문자열로 교체
    "ST":            _POS["putaway"],     # ← 실제 문자열로 교체
    "STO":           _POS["putaway"],     # ← 실제 문자열로 교체
    "putaway":       _POS["putaway"],     # ← 실제 문자열로 교체

    # ── 출하존 (Outbound / Shipping) ─────────────────────────────────────
    "outbound":      _POS["outbound"],    # ← 실제 문자열로 교체
    "OB":            _POS["outbound"],    # ← 실제 문자열로 교체

    # ── 컨베이어존 (Conveyor) ─────────────────────────────────────────────
    "conveyor":      _POS["conv_pickup"], # ← 실제 문자열로 교체
    "CV":            _POS["conv_pickup"], # ← 실제 문자열로 교체
    "CONV":          _POS["conv_pickup"], # ← 실제 문자열로 교체

    # ── 미확인 / 추가 필요 ────────────────────────────────────────────────
    # Management Service 에서 새 location 문자열이 나오면 여기에 추가:
    # "???": _POS["..."],
}

# TAT 이동 중임을 나타내는 task_state 정수 집합 (AmrTaskState proto enum)
# 0=UNSPECIFIED, 1=IDLE, 10=FAILED → 정지.  2~9 → 이동/작업 중.
TAT_MOVING_STATES: frozenset[int] = frozenset(range(2, 10))
