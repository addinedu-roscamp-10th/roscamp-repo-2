"""POST /infer endpoint — FastAPI TestClient 로 multipart 전송 + 응답 + 디스크 저장 검증."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_inspection_root: Path, fixed_round_robin) -> TestClient:  # noqa: ARG001
    """ai_service.main:app 을 lifespan 포함하여 TestClient 로 노출."""
    from ai_service.main import app

    with TestClient(app) as c:
        yield c


def test_health_endpoint(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "ai-mock"


def test_infer_saves_image_and_returns_round_robin(client: TestClient, tmp_inspection_root: Path) -> None:
    files = {"image": ("test.jpg", b"\xff\xd8\xffJPEG-payload", "image/jpeg")}

    r1 = client.post("/infer", files=files, data={"item_id": "101", "label": "tof2"})
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["ok"] is True
    assert body1["item_id"] == 101
    assert body1["result"] == "OK"  # round_robin counter=1 → OK
    assert body1["is_defective"] is False
    assert body1["model_id"] == 1
    assert body1["model_type"] == "YOLO"
    assert body1["predicted_class"] in {"CMH", "RMH", "EMH"}
    saved = Path(body1["saved_path"])
    assert saved.exists() and saved.parent.name == "101"

    r2 = client.post("/infer", files=files, data={"item_id": "101"})
    assert r2.status_code == 200
    assert r2.json()["result"] == "NG"  # counter=2 → NG

    metrics = client.get("/metrics").json()
    assert metrics["total"] == 2
    assert metrics["ok"] == 1
    assert metrics["ng"] == 1

    item_dir = tmp_inspection_root / "101"
    assert item_dir.exists()
    assert len(list(item_dir.glob("*.jpg"))) == 2


def test_infer_rejects_invalid_item_id(client: TestClient) -> None:
    r = client.post(
        "/infer",
        files={"image": ("x.jpg", b"x", "image/jpeg")},
        data={"item_id": "0"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "item_id_must_be_positive"


def test_infer_rejects_empty_image(client: TestClient) -> None:
    r = client.post(
        "/infer",
        files={"image": ("x.jpg", b"", "image/jpeg")},
        data={"item_id": "5"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "image_bytes_empty"
