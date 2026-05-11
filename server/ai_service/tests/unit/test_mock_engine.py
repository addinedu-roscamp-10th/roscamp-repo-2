"""MockInferenceEngine 결정 정책 검증."""

from __future__ import annotations

import pytest

from ai_service.mock_engine import MockInferenceEngine


def test_round_robin_alternates_ok_and_ng() -> None:
    engine = MockInferenceEngine(mode="round_robin")
    results = [engine.infer(item_id=i).result for i in range(1, 7)]
    # counter 1,3,5 → OK / counter 2,4,6 → NG (counter%2==0 이 defective)
    assert results == ["OK", "NG", "OK", "NG", "OK", "NG"]


def test_always_pass_returns_only_ok() -> None:
    engine = MockInferenceEngine(mode="always_pass")
    for _ in range(10):
        inf = engine.infer(item_id=1)
        assert inf.result == "OK"
        assert inf.is_defective is False


def test_always_fail_returns_only_ng() -> None:
    engine = MockInferenceEngine(mode="always_fail")
    for _ in range(10):
        inf = engine.infer(item_id=1)
        assert inf.result == "NG"
        assert inf.is_defective is True


def test_random_respects_pass_ratio_bounds() -> None:
    engine = MockInferenceEngine(mode="random", pass_ratio=1.0)
    assert all(engine.infer(item_id=1).result == "OK" for _ in range(20))

    engine = MockInferenceEngine(mode="random", pass_ratio=0.0)
    assert all(engine.infer(item_id=1).result == "NG" for _ in range(20))


@pytest.mark.parametrize("invalid_ratio", [-0.5, 1.5, "garbage"])
def test_pass_ratio_invalid_falls_back_to_default(invalid_ratio) -> None:
    engine = MockInferenceEngine(mode="random", pass_ratio=invalid_ratio)
    # invalid 값은 0..1 로 clamp 또는 default(0.7) 적용 — 어쨌든 호출 가능해야 함
    inf = engine.infer(item_id=1)
    assert inf.result in {"OK", "NG"}


def test_inference_payload_includes_db_schema_fields() -> None:
    engine = MockInferenceEngine(mode="always_pass")
    payload = engine.infer(item_id=42).to_payload()
    expected_keys = {
        "result",
        "is_defective",
        "predicted_class",
        "yolo_confidence",
        "anomaly_score",
        "anomaly_threshold",
        "model_id",
        "model_nm",
        "model_type",
        "step_type",
        "started_at",
        "completed_at",
    }
    assert expected_keys <= set(payload.keys())
    assert payload["predicted_class"] in {"CMH", "RMH", "EMH"}
    assert payload["model_type"] in {"YOLO", "PATCHCORE"}
    assert payload["step_type"] in {"CLASSIFICATION", "ANOMALY_DETECTION"}
