"""Mock seed loader — JSON 시나리오에서 마스터 데이터 로드.

원래 DB 마스터 테이블(res, chg_location_stat, tat_nav_pose_master, trans_task_bat_threshold)
에서 와야 하는 값들을 JSON 파일로 분리해서 보관. mock 모드와 운영 모드 모두 같은 파일을 본다.

사용자가 시나리오별로 다른 JSON을 만들어 MGMT_MOCK_SCENARIO 환경변수로 선택할 수 있다.

스키마:
{
    "resources": [{res_id, res_type, x, y, battery_pct, status}, ...],
    "charger_slots": [{row, col, res_id, status}, ...],
    "transfer_points": {"ToCAST": [x, y], ...},
    "battery_thresholds": {"ToPP": int, "ToSTRG": int, "ToSHIP": int}
}
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any

from services.contracts.enums import TaskType

logger = logging.getLogger(__name__)

_SCENARIOS_DIR = pathlib.Path(__file__).parent / "scenarios"


def _resolve_path(scenario: str) -> pathlib.Path:
    env_path = os.environ.get("MGMT_MOCK_SCENARIO_PATH")
    if env_path:
        p = pathlib.Path(env_path)
        if p.exists():
            return p
        logger.warning("MGMT_MOCK_SCENARIO_PATH=%s 가 존재하지 않음. 시나리오 디렉토리에서 검색", env_path)

    p = _SCENARIOS_DIR / f"{scenario}.json"
    if p.exists():
        return p

    fallback = _SCENARIOS_DIR / "default.json"
    logger.warning("scenario %s 못 찾음, default 로 fallback", scenario)
    return fallback


def load_scenario(name: str | None = None) -> dict[str, Any]:
    scenario = (name or os.environ.get("MGMT_MOCK_SCENARIO") or "default").strip()
    path = _resolve_path(scenario)

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    thresholds = {}
    for key, value in (raw.get("battery_thresholds") or {}).items():
        try:
            thresholds[TaskType(key)] = int(value)
        except (ValueError, KeyError):
            logger.warning("battery_thresholds: 알 수 없는 TaskType '%s' 무시", key)

    transfer_points = {
        key: (float(coords[0]), float(coords[1]))
        for key, coords in (raw.get("transfer_points") or {}).items()
        if isinstance(coords, (list, tuple)) and len(coords) >= 2
    }

    result = {
        "resources": list(raw.get("resources") or []),
        "charger_slots": list(raw.get("charger_slots") or []),
        "transfer_points": transfer_points,
        "battery_thresholds": thresholds,
    }
    logger.info("seed scenario loaded: %s (resources=%d, chargers=%d)",
                path, len(result["resources"]), len(result["charger_slots"]))
    return result
