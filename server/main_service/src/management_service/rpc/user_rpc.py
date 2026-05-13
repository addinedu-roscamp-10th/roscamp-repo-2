"""User / Operator RPC methods."""

from __future__ import annotations

import management_pb2  # type: ignore
from db_session import SessionLocal
from smart_cast_db.models import UserAccount


class UserRpcMixin:
    """User / Operator 조회 RPCs."""

    def ListOperators(self, request, context):
        role_filter = (request.role_filter or "").strip() or None
        with SessionLocal() as db:
            q = db.query(UserAccount)
            if role_filter:
                q = q.filter(UserAccount.role == role_filter)
            rows = q.order_by(UserAccount.user_nm).all()
        operators = [
            management_pb2.OperatorRow(
                user_id=row.user_id,
                user_nm=row.user_nm or "",
                email=row.email or "",
                role=row.role or "",
            )
            for row in rows
        ]
        return management_pb2.ListOperatorsResponse(operators=operators)
