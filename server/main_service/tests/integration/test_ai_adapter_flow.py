"""AIAdapter 통합 — AI 서버 호출 한 사이클 검증.

2026-05-13 (P2.4): AIAdapter 는 더 이상 DB 를 직접 갱신하지 않는다.
추론 결과는 AdapterResult.payload["inference"] dict 로만 반환되며,
DB 영속화(insp_task_txn / ai_inference_txn / insp_stat / item.is_defective 4-table)
는 호출자(task_executor → state_manager.record_inspection_result) 가 담당한다.

AI 서버는 같은 프로세스에서 uvicorn 으로 띄워 실 HTTP 경로를 통과시킨다.
INSP_COMPLETED publish 책임은 본 어댑터가 아닌 conv_adapter
(ToPAWait/CONV_ALLOW_MOVE) 가 담당하므로 publish 발생 안 함을 검증한다.
conv_adapter 의 publish 검증은 test_conv_adapter_publish.py 참조.
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
    """uvicorn 으로 ai_service mock 을 background 스레드 시작."""
    import sys

    ai_src = Path(__file__).resolve().parents[3] / "ai_service" / "src"
    if str(ai_src) not in sys.path:
        sys.path.insert(0, str(ai_src))

    monkeypatch.setenv("AI_INSP_IMAGE_ROOT", str(tmp_path / "Inspection_Image"))
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
    monkeypatch.setenv("MGMT_INSP_IMAGE_SAVE_DIR", str(tmp_path / "mgmt_insp"))

    yield port

    server.should_exit = True
    thread.join(timeout=2.0)


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
) -> None:
    """P2.4: AIAdapter 는 추론 결과 dict 만 반환하며 DB / EventBridge 를 건드리지 않는다."""
    from services.contracts.enums import EventType
    from services.contracts.models import Event
    from services.core.adapters.ai_adapter import AIAdapter
    from services.core.event_bridge import EventBridgeImpl

    saved_path = _save_image(tmp_path, jpeg_bytes, item_id=777)

    # EventBridge spy — AIAdapter 가 publish 하지 않아야 함
    event_bridge = EventBridgeImpl()
    captured: list[Event] = []
    event_bridge.subscribe(
        EventType.INSP_COMPLETED,
        lambda evt: captured.append(evt),
        subscriber_name="test_spy",
    )

    adapter = AIAdapter()  # result_command 의존성 제거
    payload = json.dumps(
        {
            "image_path": str(saved_path),
            "captured_at": time.time(),
            "label": "tof2_exit",
        }
    ).encode("utf-8")

    result = adapter.execute(item_id=777, _robot_id="AI", _command="AI_INFERENCE_REQUEST", payload=payload)

    assert result.success is True
    assert result.message == "ai_inference_completed"
    inference = result.payload["inference"]
    assert inference["ok"] is True
    assert isinstance(inference["is_defective"], bool)
    assert inference["predicted_class"] in {"CMH", "RMH", "EMH"}
    assert inference["model_id"] == 1
    assert inference["model_type"] == "YOLO"
    assert inference["yolo_confidence"] is not None
    assert inference["anomaly_score"] is not None
    assert "raw_payload" in inference
    # 4-table 갱신용 시계열 키도 포함
    assert "started_at" in inference and "completed_at" in inference

    # AIAdapter 는 EventBridge 에 publish 하지 않는다 (conv_adapter 가 담당)
    assert captured == []


def test_ai_adapter_failure_returns_error_payload_without_db_or_publish(
    ai_mock_server: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2.4: AI 서버 호출 실패 시 inference.ok=False payload 만 반환, DB 호출 없음."""
    from services.contracts.enums import EventType
    from services.core.adapters.ai_adapter import AIAdapter
    from services.core.event_bridge import EventBridgeImpl

    monkeypatch.setenv("MGMT_AI_PORT", "65501")
    monkeypatch.setenv("MGMT_AI_TIMEOUT_SEC", "0.3")
    monkeypatch.setenv("MGMT_AI_RETRY_COUNT", "0")

    saved_path = _save_image(tmp_path, b"\xff\xd8mock\xff\xd9", item_id=314)

    event_bridge = EventBridgeImpl()
    seen: list = []
    event_bridge.subscribe(
        EventType.INSP_COMPLETED,
        lambda evt: seen.append(evt),
        subscriber_name="test_spy",
    )

    adapter = AIAdapter()
    payload = json.dumps({"image_path": str(saved_path)}).encode("utf-8")
    result = adapter.execute(item_id=314, _robot_id="AI", _command="AI_INFERENCE_REQUEST", payload=payload)

    assert result.success is False
    assert "http_error" in (result.message or "")
    inference = result.payload["inference"]
    assert inference["ok"] is False
    assert "http_error" in (inference.get("error_reason") or "")
    # AIAdapter 는 EventBridge publish 하지 않으며 DB 도 건드리지 않는다
    assert seen == []


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
