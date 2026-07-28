"""AIAdapter 통합 — AI 서버 호출 한 사이클 검증 (옵션 B 정합).

2026-05-13 (P2.4): AIAdapter 는 더 이상 DB 를 직접 갱신하지 않는다.
추론 결과는 AdapterResult.payload["inference"] dict 로만 반환되며,
DB 영속화(insp_task_txn / ai_inference_txn / insp_stat / item.is_defective 4-table)
는 호출자(task_executor → state_manager.record_inspection_result) 가 담당한다.

2026-05-14 (옵션 B): AI 서버 endpoint = /predict, multipart `file` + `model` (cate_cd).
AIAdapter 는 item → ord_detail → product.cate_cd JOIN 으로 cate_cd 동적 도출 후 송신.
응답 PredictResponse {pred_label, pred_score, segmented_image, result_image} 를
AiInferenceResult 로 정규화.

AI 서버는 같은 프로세스에서 uvicorn 으로 띄워 실 HTTP 경로를 통과시킨다.
INSP_COMPLETED publish 책임은 본 어댑터가 아닌 conv_adapter
(ToPAWait/CONV_ALLOW_MOVE) 가 담당하므로 publish 발생 안 함을 검증한다.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def ai_mock_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """uvicorn 으로 ai_service mock 을 background 스레드 시작 (옵션 B /predict)."""
    import sys

    ai_src = Path(__file__).resolve().parents[3] / "ai_service" / "src"
    if str(ai_src) not in sys.path:
        sys.path.insert(0, str(ai_src))

    monkeypatch.setenv("AI_MOCK_MODE", "round_robin")

    import uvicorn
    from ai_service.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # 준비 대기 — /health 200 응답까지 최대 5초
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.3)
            if r.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        pytest.fail("ai_service mock did not become healthy in 5s")

    monkeypatch.setenv("MGMT_AI_HOST", "127.0.0.1")
    monkeypatch.setenv("MGMT_AI_PORT", str(port))
    monkeypatch.setenv("MGMT_AI_INFER_PATH", "/predict")
    monkeypatch.setenv("MGMT_INSP_IMAGE_SAVE_DIR", str(tmp_path / "mgmt_insp"))

    yield port

    server.should_exit = True
    thread.join(timeout=2.0)


@pytest.fixture()
def patched_cate_cd(monkeypatch: pytest.MonkeyPatch):
    """AIAdapter._resolve_cate_cd 를 우회 — DB 의존성 없이 테스트.

    fixture 반환값은 (item_id → cate_cd) 매핑 dict — 테스트가 갱신 가능.
    """
    from services.core.adapters.ai_adapter import AIAdapter

    item_map: dict[int, str | None] = {}

    def fake_resolve(self, item_id: int) -> str | None:
        return item_map.get(int(item_id))

    monkeypatch.setattr(AIAdapter, "_resolve_cate_cd", fake_resolve)
    return item_map


def _save_image(tmp_path: Path, image_bytes: bytes, item_id: int) -> Path:
    """main_service 디스크 저장 helper — UploadInspectionImage 가 호출할 경로 시뮬레이션."""
    from services.command.inspection_image_sink_command import InspectionImageSinkCommand

    sink = InspectionImageSinkCommand(root=tmp_path / "mgmt_insp")
    saved = sink.save(item_id=item_id, image_bytes=image_bytes)
    assert saved is not None
    return saved.path


def test_ai_adapter_success_returns_inference_payload_without_db_or_publish(
    ai_mock_server: int,
    tmp_path: Path,
    jpeg_bytes: bytes,
    patched_cate_cd: dict[int, str],
) -> None:
    """P2.4 + 옵션 B: AIAdapter 는 추론 결과 dict 만 반환하며 DB / EventBridge 를 건드리지 않는다.

    옵션 B 정합:
        - 송신 model = cate_cd (테스트는 CMH 매핑)
        - 응답 pred_label/pred_score → is_defective / anomaly_score 파생
        - predicted_class = 송신 cate_cd 그대로 보존
        - model_type = "PATCHCORE", step_type = "CLASSIFICATION"
        - yolo_confidence / anomaly_threshold / model_id 는 옵션 B 응답 미제공 → None
    """
    from services.contracts.enums import EventType
    from services.contracts.models import Event
    from services.core.adapters.ai_adapter import AIAdapter
    from services.core.event_bridge import EventBridgeImpl

    item_id = 777
    patched_cate_cd[item_id] = "CMH"
    saved_path = _save_image(tmp_path, jpeg_bytes, item_id=item_id)

    # EventBridge spy — AIAdapter 가 publish 하지 않아야 함
    event_bridge = EventBridgeImpl()
    captured: list[Event] = []
    event_bridge.subscribe(
        EventType.INSP_COMPLETED,
        lambda evt: captured.append(evt),
        subscriber_name="test_spy",
    )

    adapter = AIAdapter()
    payload = json.dumps({"image_path": str(saved_path)}).encode("utf-8")
    result = adapter.execute(
        item_id=item_id,
        _robot_id="AI",
        _command="AI_INFERENCE_REQUEST",
        payload=payload,
    )

    assert result.success is True
    assert result.message == "ai_inference_completed"
    inference = result.payload["inference"]
    assert inference["ok"] is True
    assert isinstance(inference["is_defective"], bool)

    # 옵션 B: 송신 model (=cate_cd) 이 predicted_class 로 보존
    assert inference["predicted_class"] == "CMH"
    assert inference["model_type"] == "PATCHCORE"
    assert inference["step_type"] == "CLASSIFICATION"
    assert inference["anomaly_score"] is not None
    # 옵션 B 응답 미제공 필드
    assert inference["yolo_confidence"] is None
    assert inference["anomaly_threshold"] is None
    assert inference["model_id"] is None

    # 4-table 갱신용 시계열 키
    assert "started_at" in inference and "completed_at" in inference

    # raw_payload: base64 이미지는 strip 마커로 치환됨 (DB result_json 크기 절약)
    raw = inference["raw_payload"]
    assert raw.get("pred_label") in ("Normal", "Anomalous")
    assert isinstance(raw.get("pred_score"), (int, float))
    assert isinstance(raw.get("segmented_image"), str)
    assert raw["segmented_image"].startswith("<base64 omitted")
    assert raw["result_image"].startswith("<base64 omitted")

    # AIAdapter 는 EventBridge 에 publish 하지 않는다 (conv_adapter 가 담당)
    assert captured == []


def test_ai_adapter_failure_returns_error_payload_without_db_or_publish(
    ai_mock_server: int,
    tmp_path: Path,
    patched_cate_cd: dict[int, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """옵션 B: AI 서버 호출 실패 시 inference.ok=False payload 만 반환, DB 호출 없음."""
    from services.contracts.enums import EventType
    from services.core.adapters.ai_adapter import AIAdapter
    from services.core.event_bridge import EventBridgeImpl

    # ai_mock_server 가 떴어도 PORT 를 죽은 곳으로 redirect → unreachable
    monkeypatch.setenv("MGMT_AI_PORT", "65501")
    monkeypatch.setenv("MGMT_AI_TIMEOUT_SEC", "0.3")
    monkeypatch.setenv("MGMT_AI_RETRY_COUNT", "0")

    item_id = 314
    patched_cate_cd[item_id] = "RMH"
    saved_path = _save_image(tmp_path, b"\xff\xd8mock\xff\xd9", item_id=item_id)

    event_bridge = EventBridgeImpl()
    seen: list = []
    event_bridge.subscribe(
        EventType.INSP_COMPLETED,
        lambda evt: seen.append(evt),
        subscriber_name="test_spy",
    )

    adapter = AIAdapter()
    payload = json.dumps({"image_path": str(saved_path)}).encode("utf-8")
    result = adapter.execute(
        item_id=item_id,
        _robot_id="AI",
        _command="AI_INFERENCE_REQUEST",
        payload=payload,
    )

    assert result.success is False
    assert "http_error" in (result.message or "")
    inference = result.payload["inference"]
    assert inference["ok"] is False
    assert "http_error" in (inference.get("error_reason") or "")
    assert seen == []


def test_ai_adapter_rejects_missing_cate_cd(
    ai_mock_server: int,
    tmp_path: Path,
    jpeg_bytes: bytes,
    patched_cate_cd: dict[int, str],
) -> None:
    """item_id 에 매핑되는 product.cate_cd 가 없으면 inference 자체를 시도하지 않고 실패."""
    from services.core.adapters.ai_adapter import AIAdapter

    item_id = 999
    # patched_cate_cd 에 등록 안 함 → None 반환
    saved_path = _save_image(tmp_path, jpeg_bytes, item_id=item_id)

    adapter = AIAdapter()
    payload = json.dumps({"image_path": str(saved_path)}).encode("utf-8")
    result = adapter.execute(
        item_id=item_id,
        _robot_id="AI",
        _command="AI_INFERENCE_REQUEST",
        payload=payload,
    )

    assert result.success is False
    assert "cate_cd_invalid_or_missing" in (result.message or "")


def test_ai_adapter_rejects_missing_image_path(jpeg_bytes: bytes) -> None:
    from services.core.adapters.ai_adapter import AIAdapter

    adapter = AIAdapter()
    r = adapter.execute(item_id=1, _robot_id="AI", _command="AI_INFERENCE_REQUEST", payload=b"{}")
    assert r.success is False
    assert r.message == "image_path_or_url_required"

    r = adapter.execute(
        item_id=1,
        _robot_id="AI",
        _command="AI_INFERENCE_REQUEST",
        payload=json.dumps({"image_path": "/nope/missing.jpg"}).encode("utf-8"),
    )
    assert r.success is False
    assert r.message.startswith("image_path_not_found")


def test_ai_adapter_invalid_json_payload() -> None:
    from services.core.adapters.ai_adapter import AIAdapter

    adapter = AIAdapter()
    r = adapter.execute(item_id=1, _robot_id="AI", _command="AI_INFERENCE_REQUEST", payload=b"not json")
    assert r.success is False
    assert r.message == "invalid_json_payload"
