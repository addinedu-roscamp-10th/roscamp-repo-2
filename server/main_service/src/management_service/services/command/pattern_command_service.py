"""Write-side pattern registration commands for Management gRPC."""

from __future__ import annotations

from dataclasses import dataclass

from smart_cast_db.database import SessionLocal
from smart_cast_db.models import Ord, OrdPattern


@dataclass(frozen=True)
class PatternCommandRow:
    ord_id: int
    pattern_id: int | None
    ptn_loc_id: int | None


class PatternCommandService:
    """Create or update order-to-pattern assignments."""

    def register_pattern(self, ord_id: int, ptn_loc_id: int) -> PatternCommandRow:
        if ptn_loc_id not in (1, 2, 3):
            raise ValueError("ptn_loc_id must be between 1 and 3")

        with SessionLocal() as db:
            if not db.get(Ord, ord_id):
                raise LookupError(f"ord_id={ord_id} not found")

            existing = db.get(OrdPattern, ord_id)
            if existing is None:
                existing = OrdPattern(ord_id=ord_id, ptn_loc_id=ptn_loc_id)
                db.add(existing)
            else:
                existing.ptn_loc_id = ptn_loc_id

            db.commit()
            db.refresh(existing)
            return PatternCommandRow(
                ord_id=existing.ord_id,
                pattern_id=existing.pattern_id,
                ptn_loc_id=existing.ptn_loc_id,
            )
