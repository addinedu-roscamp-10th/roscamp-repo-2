"""Read-side pattern queries for Management gRPC handlers."""

from __future__ import annotations

from dataclasses import dataclass

from smart_cast_db.database import SessionLocal
from smart_cast_db.models import OrdPattern


@dataclass(frozen=True)
class PatternQueryRow:
    ord_id: int
    pattern_id: int | None
    ptn_loc_id: int | None


class PatternQueryService:
    """Read-only pattern assignment lookups."""

    def list_patterns(self) -> list[PatternQueryRow]:
        with SessionLocal() as db:
            rows = db.query(OrdPattern).order_by(OrdPattern.ord_id.asc()).all()
            return [
                PatternQueryRow(
                    ord_id=row.ord_id,
                    pattern_id=row.pattern_id,
                    ptn_loc_id=row.ptn_loc_id,
                )
                for row in rows
            ]

    def get_pattern(self, ord_id: int) -> PatternQueryRow | None:
        with SessionLocal() as db:
            row = db.get(OrdPattern, ord_id)
            if row is None:
                return None
            return PatternQueryRow(
                ord_id=row.ord_id,
                pattern_id=row.pattern_id,
                ptn_loc_id=row.ptn_loc_id,
            )
