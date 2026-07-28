"""User / Operator RPC methods."""

from __future__ import annotations

import grpc
import management_pb2  # type: ignore
from smart_cast_db.database import SessionLocal
from smart_cast_db.models import UserAccount


def _operator_proto(row: UserAccount) -> management_pb2.OperatorRow:
    return management_pb2.OperatorRow(
        user_id=row.user_id,
        user_nm=row.user_nm or "",
        email=row.email or "",
        role=row.role or "",
    )


class UserRpcMixin:
    """User / Operator 조회 RPCs."""

    def ListOperators(self, request, context):
        role_filter = (request.role_filter or "").strip() or None
        with SessionLocal() as db:
            q = db.query(UserAccount)
            if role_filter:
                q = q.filter(UserAccount.role == role_filter)
            rows = q.order_by(UserAccount.user_nm).all()
        operators = [_operator_proto(row) for row in rows]
        return management_pb2.ListOperatorsResponse(operators=operators)

    def GetOperatorByEmail(self, request, context):
        email = (request.email or "").strip()
        if not email:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "email is required")

        with SessionLocal() as db:
            row = db.query(UserAccount).filter(UserAccount.email == email).first()
        if row is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"operator email={email!r} not found")
        return _operator_proto(row)
