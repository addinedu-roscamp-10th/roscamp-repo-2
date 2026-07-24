"""품질 화면 snapshot용 읽기 전용 조회."""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy import desc, func

from smart_cast_db.database import SessionLocal
from smart_cast_db.models import (
    AiInferenceTxn,
    Category,
    InspStat,
    InspTaskTxn,
    Item,
    OrdDetail,
    Product,
)


@dataclass(frozen=True)
class QualityStatsQueryRow:
    inspected: int
    good: int
    defective: int
    pending: int
    defect_rate: float


@dataclass(frozen=True)
class QualityInspectionQueryRow:
    txn_id: int
    item_id: int
    inference_id: int
    txn_stat: str
    result: str
    req_at: str
    start_at: str
    end_at: str
    inspected_at: str
    product: str
    defect_type: str
    inspector: str
    note: str
    confidence: float


@dataclass(frozen=True)
class DefectRateTrendQueryRow:
    label: str
    rate: float


@dataclass(frozen=True)
class QualitySnapshotQueryResult:
    stats: QualityStatsQueryRow
    inspections: list[QualityInspectionQueryRow]
    trend: list[DefectRateTrendQueryRow]


@dataclass(frozen=True)
class InspectionImageQueryResult:
    image_bytes: bytes
    content_type: str


class QualityQueryService:
    """품질 snapshot과 검사 결과 이미지를 조회."""

    def __init__(
        self,
        session_factory=SessionLocal,
        *,
        image_root: str | Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._image_root = Path(
            image_root
            or os.environ.get(
                "MGMT_INSP_IMAGE_SAVE_DIR",
                "/var/lib/casting/inspections",
            )
        )

    def get_snapshot(
        self,
        *,
        hours: int = 24,
        inspection_limit: int = 200,
    ) -> QualitySnapshotQueryResult:
        with self._session_factory() as db:
            return QualitySnapshotQueryResult(
                stats=self._get_stats(db),
                inspections=self._list_recent_inspections(db, limit=inspection_limit),
                trend=self._get_defect_rate_trend(db, hours=hours),
            )

    @staticmethod
    def _get_stats(db) -> QualityStatsQueryRow:
        inspected = (
            db.query(func.count(Item.item_id))
            .filter(Item.is_defective.isnot(None))
            .scalar()
            or 0
        )
        good = (
            db.query(func.count(Item.item_id))
            .filter(Item.is_defective.is_(False))
            .scalar()
            or 0
        )
        defective = (
            db.query(func.count(Item.item_id))
            .filter(Item.is_defective.is_(True))
            .scalar()
            or 0
        )
        pending = (
            db.query(func.count(Item.item_id))
            .filter(Item.is_defective.is_(None))
            .scalar()
            or 0
        )
        defect_rate = defective / inspected * 100.0 if inspected else 0.0
        return QualityStatsQueryRow(
            inspected=int(inspected),
            good=int(good),
            defective=int(defective),
            pending=int(pending),
            defect_rate=round(defect_rate, 2),
        )

    @staticmethod
    def _list_recent_inspections(
        db,
        *,
        limit: int,
    ) -> list[QualityInspectionQueryRow]:
        inference_id = func.coalesce(
            InspTaskTxn.final_inference_id,
            InspStat.patchcore_inference_id,
            InspStat.yolo_inference_id,
        )
        rows = (
            db.query(
                InspTaskTxn,
                InspStat,
                AiInferenceTxn,
                Category,
            )
            .outerjoin(InspStat, InspStat.insp_txn_id == InspTaskTxn.txn_id)
            .outerjoin(
                AiInferenceTxn,
                AiInferenceTxn.inference_id == inference_id,
            )
            .outerjoin(Item, Item.item_id == InspTaskTxn.item_id)
            .outerjoin(OrdDetail, OrdDetail.ord_id == Item.ord_id)
            .outerjoin(Product, Product.prod_id == OrdDetail.prod_id)
            .outerjoin(Category, Category.cate_cd == Product.cate_cd)
            .order_by(desc(InspTaskTxn.req_at), desc(InspTaskTxn.txn_id))
            .limit(max(1, limit))
            .all()
        )

        inspections: list[QualityInspectionQueryRow] = []
        for txn, stat, inference, category in rows:
            result = _inspection_result(txn, stat)
            inspected_at = txn.end_at or txn.start_at or txn.req_at
            inspections.append(
                QualityInspectionQueryRow(
                    txn_id=int(txn.txn_id),
                    item_id=int(txn.item_id or 0),
                    inference_id=(
                        int(inference.inference_id)
                        if inference is not None
                        else 0
                    ),
                    txn_stat=txn.txn_stat or "",
                    result=result,
                    req_at=_iso(txn.req_at),
                    start_at=_iso(txn.start_at),
                    end_at=_iso(txn.end_at),
                    inspected_at=_iso(inspected_at),
                    product=category.cate_nm if category and category.cate_nm else "",
                    defect_type="",
                    inspector="",
                    note="",
                    confidence=(
                        float(inference.confidence)
                        if inference and inference.confidence is not None
                        else 0.0
                    ),
                )
            )
        return inspections

    def get_inspection_image(
        self,
        *,
        inference_id: int,
        kind: str,
    ) -> InspectionImageQueryResult:
        """추론 결과 이미지 파일을 gRPC 응답용 bytes로 조회."""
        if inference_id <= 0:
            raise ValueError("inference_id must be positive")
        if kind not in {"result", "segmented"}:
            raise ValueError(f"unsupported inspection image kind={kind!r}")

        with self._session_factory() as db:
            inference = db.get(AiInferenceTxn, inference_id)

        if inference is None:
            raise LookupError(f"ai_inference_txn={inference_id} not found")

        image_url = (
            inference.result_image_url
            if kind == "result"
            else inference.segmented_image_url
        )
        if not image_url:
            raise LookupError(
                f"ai_inference_txn={inference_id} {kind} image not found"
            )

        image_path = _image_path_from_url(self._image_root, image_url)
        try:
            image_bytes = image_path.read_bytes()
        except FileNotFoundError as exc:
            raise LookupError(
                f"inspection image file not found: {image_path}"
            ) from exc
        if not image_bytes:
            raise LookupError(f"inspection image file is empty: {image_path}")

        content_type = mimetypes.guess_type(image_path.name)[0]
        return InspectionImageQueryResult(
            image_bytes=image_bytes,
            content_type=content_type or "application/octet-stream",
        )

    @staticmethod
    def _get_defect_rate_trend(
        db,
        *,
        hours: int,
    ) -> list[DefectRateTrendQueryRow]:
        since = datetime.now() - timedelta(hours=max(1, hours))
        bucket = func.date_trunc("hour", Item.updated_at).label("bucket")
        rows = (
            db.query(
                bucket,
                func.count(Item.item_id).label("inspected"),
                func.count(Item.item_id)
                .filter(Item.is_defective.is_(True))
                .label("defective"),
            )
            .filter(Item.updated_at >= since)
            .filter(Item.is_defective.isnot(None))
            .group_by(bucket)
            .order_by(bucket)
            .all()
        )
        return [
            DefectRateTrendQueryRow(
                label=row.bucket.strftime("%m/%d %H시"),
                rate=round(
                    (int(row.defective or 0) / int(row.inspected or 1)) * 100.0,
                    2,
                ),
            )
            for row in rows
        ]


def _inspection_result(txn: Any, stat: Any) -> str:
    if stat is not None and stat.final_result == "GP":
        return "OK"
    if stat is not None and stat.final_result == "DP":
        return "NG"
    if txn.result is True:
        return "OK"
    if txn.result is False:
        return "NG"
    return ""


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _image_path_from_url(root: Path, image_url: str) -> Path:
    """HttpImageServer URL을 같은 저장소의 로컬 파일 경로로 변환."""
    url_path = unquote(urlsplit(image_url).path)
    prefix = "/inspections/"
    if not url_path.startswith(prefix):
        raise LookupError(f"unsupported inspection image URL: {image_url}")

    relative_path = Path(url_path[len(prefix) :])
    resolved_root = root.resolve()
    resolved_path = (resolved_root / relative_path).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise LookupError(f"inspection image path escapes root: {image_url}")
    return resolved_path
