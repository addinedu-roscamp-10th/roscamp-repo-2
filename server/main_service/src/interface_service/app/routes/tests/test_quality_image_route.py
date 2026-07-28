from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clients.management import (
    InspectionImageNotFound,
    ManagementClient,
    ManagementUnavailable,
    get_management_client,
)
from app.routes import quality


class _FakeManagementClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.error: Exception | None = None

    def get_inspection_image(
        self,
        inference_id: int,
        *,
        kind: str = "result",
    ) -> dict[str, bytes | str]:
        self.calls.append((inference_id, kind))
        if self.error is not None:
            raise self.error
        return {
            "image_bytes": b"\x89PNG\r\n",
            "content_type": "image/png",
        }


def _client(fake: _FakeManagementClient) -> TestClient:
    app = FastAPI()
    app.include_router(quality.router)
    app.dependency_overrides[get_management_client] = lambda: fake
    return TestClient(app)


def test_inspection_image_proxies_management_grpc_bytes() -> None:
    fake = _FakeManagementClient()

    response = _client(fake).get(
        "/api/quality/inspections/31/image",
        params={"kind": "segmented"},
    )

    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n"
    assert response.headers["content-type"] == "image/png"
    assert fake.calls == [(31, "segmented")]


def test_inspection_image_maps_missing_image_to_404() -> None:
    fake = _FakeManagementClient()
    fake.error = InspectionImageNotFound("result image not found")

    response = _client(fake).get("/api/quality/inspections/31/image")

    assert response.status_code == 404
    assert response.json()["detail"] == "result image not found"


def test_inspection_image_maps_management_outage_to_503() -> None:
    fake = _FakeManagementClient()
    fake.error = ManagementUnavailable("unreachable")

    response = _client(fake).get("/api/quality/inspections/31/image")

    assert response.status_code == 503
    assert "Management Service unavailable" in response.json()["detail"]


def test_management_client_builds_inspection_image_rpc_request() -> None:
    captured: dict[str, object] = {}

    class _Stub:
        def GetInspectionImage(self, request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return type(
                "_Response",
                (),
                {
                    "image_bytes": b"result-image",
                    "content_type": "image/png",
                },
            )()

    client = ManagementClient(timeout=1.25)
    client._channel = object()  # type: ignore[assignment]
    client._stub = _Stub()

    image = client.get_inspection_image(31, kind="result")

    request = captured["request"]
    assert request.inference_id == 31
    assert request.kind == 1
    assert captured["timeout"] == 1.25
    assert image == {
        "image_bytes": b"result-image",
        "content_type": "image/png",
    }
