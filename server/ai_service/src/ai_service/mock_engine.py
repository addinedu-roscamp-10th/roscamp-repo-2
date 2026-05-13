"""Mock 추론 엔진 — e2e 통합 테스트용 양/불 판정.

실 AI 모델 (YOLO / PatchCore) 가 부착되기 전 컨베이어 e2e 흐름을 검증하기 위해
양품(GP) / 불량(DP) 결과를 결정 정책에 따라 반환한다.

지원 모드 (AI_MOCK_MODE env):
    - always_pass: 항상 OK
    - always_fail: 항상 NG
    - round_robin: 호출 순서 OK, NG, OK, NG...
    - random:      랜덤 (AI_MOCK_PASS_RATIO env 로 OK 비율 조절, 기본 0.7)
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DEFAULT_MODE = "round_robin"
_DEFAULT_PASS_RATIO = 0.7
_PREDICTED_CLASSES = ("CMH", "RMH", "EMH")


@dataclass(frozen=True)
class MockInference:
    """Mock 추론 결과 — DB schema (insp_stat / ai_inference_txn) 와 정합."""

    result: str  # "OK" | "NG"
    is_defective: bool
    predicted_class: str  # "CMH" | "RMH" | "EMH"
    yolo_confidence: float
    anomaly_score: float
    anomaly_threshold: float
    model_id: int
    model_nm: str
    model_type: str  # "YOLO" | "PATCHCORE"
    step_type: str  # "CLASSIFICATION" | "ANOMALY_DETECTION"
    started_at_iso: str
    completed_at_iso: str

    def to_payload(self) -> dict[str, object]:
        return {
            "result": self.result,
            "is_defective": self.is_defective,
            "predicted_class": self.predicted_class,
            "yolo_confidence": round(self.yolo_confidence, 4),
            "anomaly_score": round(self.anomaly_score, 4),
            "anomaly_threshold": round(self.anomaly_threshold, 4),
            "model_id": self.model_id,
            "model_nm": self.model_nm,
            "model_type": self.model_type,
            "step_type": self.step_type,
            "started_at": self.started_at_iso,
            "completed_at": self.completed_at_iso,
        }


class MockInferenceEngine:
    """결정 정책에 따른 mock 양/불 판정 + thread-safe round-robin counter."""

    def __init__(self, mode: str | None = None, pass_ratio: float | None = None) -> None:
        self._mode = (mode or os.environ.get("AI_MOCK_MODE", _DEFAULT_MODE)).strip().lower()
        raw_ratio = (
            pass_ratio
            if pass_ratio is not None
            else os.environ.get("AI_MOCK_PASS_RATIO", _DEFAULT_PASS_RATIO)
        )
        try:
            ratio = float(raw_ratio)
        except (TypeError, ValueError):
            ratio = _DEFAULT_PASS_RATIO
        self._pass_ratio = max(0.0, min(1.0, ratio))
        self._rr_lock = threading.Lock()
        self._rr_counter = 0
        logger.info(
            "MockInferenceEngine 초기화: mode=%s pass_ratio=%.2f",
            self._mode,
            self._pass_ratio,
        )

    def infer(self, *, item_id: int, started_at: float | None = None) -> MockInference:
        started_at = started_at if started_at and started_at > 0 else time.time()
        is_defective = self._decide_is_defective()
        completed_at = time.time()

        # PatchCore 식 anomaly score — 양품은 낮은 score, 불량은 높은 score
        anomaly_threshold = 0.30
        anomaly_score = (
            random.uniform(0.32, 0.85) if is_defective else random.uniform(0.05, 0.28)
        )
        yolo_confidence = random.uniform(0.85, 0.99)
        predicted_class = random.choice(_PREDICTED_CLASSES)

        return MockInference(
            result="NG" if is_defective else "OK",
            is_defective=is_defective,
            predicted_class=predicted_class,
            yolo_confidence=yolo_confidence,
            anomaly_score=anomaly_score,
            anomaly_threshold=anomaly_threshold,
            model_id=1,
            model_nm="mock-yolov11-cmh",
            model_type="YOLO",
            step_type="CLASSIFICATION",
            started_at_iso=_to_iso(started_at),
            completed_at_iso=_to_iso(completed_at),
        )

    def _decide_is_defective(self) -> bool:
        if self._mode == "always_pass":
            return False
        if self._mode == "always_fail":
            return True
        if self._mode == "random":
            return random.random() >= self._pass_ratio
        # round_robin (기본): 짝수 OK, 홀수 NG
        with self._rr_lock:
            self._rr_counter += 1
            return self._rr_counter % 2 == 0


def _to_iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
