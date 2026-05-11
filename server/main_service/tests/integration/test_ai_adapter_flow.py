"""AIAdapter 통합 — AI 서버 호출 + DB 기록 + INSP_COMPLETED publish 한 사이클 검증.

DB 의존성은 InspectionResultCommand 를 mock 으로 대체하여 격리한다. AI 서버는
같은 프로세스에서 uvicorn 으로 띄워 실 HTTP 경로를 통과시킨다.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

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


def test_ai_adapter_full_cycle_publishes_insp_completed(
    ai_mock_server: int,
    tmp_path: Path,
    jpeg_bytes: bytes,
) -> None:
    from datetime import datetime, timezone

    from services.command.inspection_result_command import InspectionResultRow
    from services.contracts.enums import EventType
    from services.contracts.models import Event
    from services.core.adapters.ai_adapter import AIAdapter
    from services.core.event_bridge import EventBridgeImpl

    # 1) 실 disk 에 검사 이미지 저장 (main_service sink)
    saved_path = _save_image(tmp_path, jpeg_bytes, item_id=777)

    # 2) DB 단은 mock — record_inspection_result 호출 인자만 검증
    result_command = MagicMock()
    result_command.record_inspection_result.return_value = InspectionResultRow(
        insp_txn_id=999,
        item_id=777,
        inference_id=8888,
        model_id=1,
        result="OK",
        is_defective=False,
        predicted_class="CMH",
        recorded_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    # 3) EventBridge 는 실제 인스턴스 — INSP_COMPLETED 수신을 spy
    event_bridge = EventBridgeImpl()
    captured: list[Event] = []
    event_bridge.subscribe(
        EventType.INSP_COMPLETED,
        lambda evt: captured.append(evt),
        subscriber_name="test_spy",
    )

    # 4) AIAdapter 는 실 AiInferenceCommand (env 로 mock 서버 가리킴) + mock DB
    adapter = AIAdapter(result_command=result_command, event_bridge=event_bridge)
    payload = json.dumps(
        {
            "image_path": str(saved_path),
            "captured_at": time.time(),
            "label": "tof2_exit",
        }
    ).encode("utf-8")

    result = adapter.execute(item_id=777, _robot_id="AI", _command="AI_INFERENCE_REQUEST", payload=payload)

    # 5) 어댑터 응답 검증
    assert result.success is True
    assert result.message == "ai_inference_recorded"
    assert result.payload["inference"]["ok"] is True
    assert result.payload["inference"]["result"] == "OK"  # round_robin 1번째 호출
    assert result.payload["inspection_result"]["insp_txn_id"] == 999
    assert result.payload["inspection_result"]["result"] == "OK"

    # 6) DB 기록 호출 인자 검증
    result_command.record_inspection_result.assert_called_once()
    call_kwargs = result_command.record_inspection_result.call_args.kwargs
    assert call_kwargs["item_id"] == 777
    assert call_kwargs["is_defective"] is False  # OK → not defective
    assert call_kwargs["predicted_class"] in {"CMH", "RMH", "EMH"}
    assert call_kwargs["model_id"] == 1
    assert call_kwargs["model_type"] == "YOLO"
    assert call_kwargs["yolo_confidence"] is not None
    assert call_kwargs["anomaly_score"] is not None

    # 7) INSP_COMPLETED 이벤트 수신 + payload 검증
    assert len(captured) == 1
    event = captured[0]
    assert event.event_type == EventType.INSP_COMPLETED
    assert event.item_id == 777
    assert event.txn_id == 999
    assert event.payload["result"] == "OK"
    assert event.payload["is_defective"] is False
    assert event.payload["insp_txn_id"] == 999


def test_ai_adapter_failure_records_failure_and_no_publish(
    ai_mock_server: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.contracts.enums import EventType
    from services.core.adapters.ai_adapter import AIAdapter
    from services.core.event_bridge import EventBridgeImpl

    # AI 서버를 다른 포트로 지정 — 의도적으로 연결 실패 유도
    monkeypatch.setenv("MGMT_AI_PORT", "65501")
    monkeypatch.setenv("MGMT_AI_TIMEOUT_SEC", "0.3")
    monkeypatch.setenv("MGMT_AI_RETRY_COUNT", "0")

    saved_path = _save_image(tmp_path, b"\xff\xd8mock\xff\xd9", item_id=314)

    result_command = MagicMock()
    event_bridge = EventBridgeImpl()
    seen: list = []
    event_bridge.subscribe(
        EventType.INSP_COMPLETED,
        lambda evt: seen.append(evt),
        subscriber_name="test_spy",
    )

    adapter = AIAdapter(result_command=result_command, event_bridge=event_bridge)
    payload = json.dumps({"image_path": str(saved_path)}).encode("utf-8")
    result = adapter.execute(item_id=314, _robot_id="AI", _command="AI_INFERENCE_REQUEST", payload=payload)

    assert result.success is False
    assert "http_error" in (result.message or "")
    # 실패 path: record_inspection_failure 만 호출, record_inspection_result 는 호출 안 됨
    result_command.record_inspection_result.assert_not_called()
    result_command.record_inspection_failure.assert_called_once()
    fail_kwargs = result_command.record_inspection_failure.call_args.kwargs
    assert fail_kwargs["item_id"] == 314
    assert "http_error" in (fail_kwargs.get("reason") or "")
    # INSP_COMPLETED publish 는 발생하지 않아야 함
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
