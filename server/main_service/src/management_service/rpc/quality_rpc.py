"""품질 화면 snapshot RPC."""

from __future__ import annotations

import grpc
import management_pb2  # type: ignore


def _iso(value) -> str:
    return value.isoformat() if value is not None else ""


class QualityRpcMixin:
    """품질 조회를 unary snapshot 한 번으로 제공."""

    def GetQualitySnapshot(self, request, context):
        snapshot = self.quality_query_service.get_snapshot(
            hours=request.hours or 24,
            inspection_limit=request.inspection_limit or 200,
        )
        return management_pb2.QualitySnapshotResponse(
            stats=management_pb2.QualityStats(
                inspected=snapshot.stats.inspected,
                good=snapshot.stats.good,
                defective=snapshot.stats.defective,
                pending=snapshot.stats.pending,
                defect_rate=snapshot.stats.defect_rate,
            ),
            inspections=[
                management_pb2.InspectionEntry(
                    txn_id=row.txn_id,
                    item_id=row.item_id,
                    inference_id=row.inference_id,
                    txn_stat=row.txn_stat,
                    result=row.result,
                    req_at=row.req_at,
                    start_at=row.start_at,
                    end_at=row.end_at,
                    inspected_at=row.inspected_at,
                    product=row.product,
                    defect_type=row.defect_type,
                    inspector=row.inspector,
                    note=row.note,
                    confidence=row.confidence,
                )
                for row in snapshot.inspections
            ],
            trend=[
                management_pb2.DefectRateTrendEntry(
                    label=row.label,
                    rate=row.rate,
                )
                for row in snapshot.trend
            ],
            defect_types=management_pb2.DefectTypeSection(
                source_available=False,
            ),
            standards=management_pb2.InspectionStandardSection(
                source_available=False,
            ),
            production_vs_defects=management_pb2.ProductionVsDefectSection(
                source_available=False,
            ),
        )

    def GetInspectionImage(self, request, context):
        if request.kind == management_pb2.INSPECTION_IMAGE_KIND_RESULT:
            kind = "result"
        elif request.kind == management_pb2.INSPECTION_IMAGE_KIND_SEGMENTED:
            kind = "segmented"
        else:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "inspection image kind is required",
            )

        try:
            image = self.quality_query_service.get_inspection_image(
                inference_id=request.inference_id,
                kind=kind,
            )
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except LookupError as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))

        return management_pb2.GetInspectionImageResponse(
            image_bytes=image.image_bytes,
            content_type=image.content_type,
        )

    def CompleteInspection(self, request, context):
        try:
            completed = self.manual_inspection_command_service.complete(
                txn_id=request.txn_id,
                result=request.result,
            )
        except LookupError as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))

        return management_pb2.InspectionEntry(
            txn_id=completed.txn_id,
            item_id=completed.item_id or 0,
            txn_stat=completed.txn_stat,
            result=completed.result,
            req_at=_iso(completed.req_at),
            start_at=_iso(completed.start_at),
            end_at=_iso(completed.end_at),
            inspected_at=_iso(completed.end_at),
        )
