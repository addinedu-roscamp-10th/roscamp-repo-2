"""Quality snapshot의 통계와 RPC 변환 검증."""

from __future__ import annotations

from types import SimpleNamespace

import management_pb2
import pytest

from rpc.quality_rpc import QualityRpcMixin
from services.query.quality_query_service import (
    DefectRateTrendQueryRow,
    InspectionImageQueryResult,
    QualityInspectionQueryRow,
    QualityQueryService,
    QualitySnapshotQueryResult,
    QualityStatsQueryRow,
)


class _ScalarQueryFake:
    def __init__(self, values: list[int]) -> None:
        self._values = iter(values)

    def query(self, *args):
        return self

    def filter(self, *args):
        return self

    def scalar(self):
        return next(self._values)


class _QualityServiceFake:
    def get_snapshot(self, *, hours: int, inspection_limit: int):
        assert hours == 24
        assert inspection_limit == 200
        return QualitySnapshotQueryResult(
            stats=QualityStatsQueryRow(
                inspected=8,
                good=6,
                defective=2,
                pending=3,
                defect_rate=25.0,
            ),
            inspections=[
                QualityInspectionQueryRow(
                    txn_id=11,
                    item_id=21,
                    inference_id=31,
                    txn_stat="SUCC",
                    result="NG",
                    req_at="2026-07-23T10:00:00",
                    start_at="2026-07-23T10:01:00",
                    end_at="2026-07-23T10:02:00",
                    inspected_at="2026-07-23T10:02:00",
                    product="원형 맨홀",
                    defect_type="",
                    inspector="",
                    note="",
                    confidence=0.91,
                )
            ],
            trend=[
                DefectRateTrendQueryRow(
                    label="07/23 10시",
                    rate=25.0,
                )
            ],
        )

    def get_inspection_image(self, *, inference_id: int, kind: str):
        assert inference_id == 31
        assert kind == "result"
        return InspectionImageQueryResult(
            image_bytes=b"image-data",
            content_type="image/png",
        )


class _QualityRpc(QualityRpcMixin):
    quality_query_service = _QualityServiceFake()


class _Request:
    hours = 24
    inspection_limit = 200


def test_quality_stats_uses_defective_ratio() -> None:
    stats = QualityQueryService._get_stats(_ScalarQueryFake([8, 6, 2, 3]))

    assert stats.inspected == 8
    assert stats.good == 6
    assert stats.defective == 2
    assert stats.defect_rate == 25.0


def test_quality_rpc_marks_only_missing_sources_unavailable() -> None:
    response = _QualityRpc().GetQualitySnapshot(_Request(), context=None)

    assert response.stats.defect_rate == 25.0
    assert response.inspections[0].result == "NG"
    assert response.inspections[0].inference_id == 31
    assert response.trend[0].rate == 25.0
    assert response.defect_types.source_available is False
    assert response.standards.source_available is False
    assert response.production_vs_defects.source_available is False
    assert list(response.defect_types.entries) == []


def test_quality_query_reads_result_image_bytes(tmp_path) -> None:
    image_path = tmp_path / "21" / "11_result.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"png-image")

    inference = SimpleNamespace(
        result_image_url=(
            "http://management:18800/inspections/21/11_result.png"
        ),
        segmented_image_url=None,
    )

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, inference_id):
            assert inference_id == 31
            return inference

    service = QualityQueryService(
        session_factory=_Session,
        image_root=tmp_path,
    )

    image = service.get_inspection_image(
        inference_id=31,
        kind="result",
    )

    assert image.image_bytes == b"png-image"
    assert image.content_type == "image/png"


def test_quality_query_rejects_missing_image(tmp_path) -> None:
    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _inference_id):
            return SimpleNamespace(
                result_image_url=None,
                segmented_image_url=None,
            )

    service = QualityQueryService(
        session_factory=_Session,
        image_root=tmp_path,
    )

    with pytest.raises(LookupError, match="result image not found"):
        service.get_inspection_image(inference_id=31, kind="result")


def test_quality_rpc_returns_result_image_bytes() -> None:
    request = management_pb2.GetInspectionImageRequest(
        inference_id=31,
        kind=management_pb2.INSPECTION_IMAGE_KIND_RESULT,
    )

    response = _QualityRpc().GetInspectionImage(request, context=None)

    assert response.image_bytes == b"image-data"
    assert response.content_type == "image/png"
