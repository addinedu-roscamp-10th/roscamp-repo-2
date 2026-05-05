"""Pattern-related Management gRPC methods."""

from __future__ import annotations

import grpc
import management_pb2  # type: ignore

from rpc.proto_helpers import pattern_to_proto


class PatternRpcMixin:
    """Pattern read RPCs. Write-side pattern registration stays in Interface for now."""

    def ListPatterns(self, request, context):
        rows = self.pattern_query_service.list_patterns()
        return management_pb2.ListPatternsResponse(
            patterns=[pattern_to_proto(row) for row in rows]
        )

    def GetPattern(self, request, context):
        row = self.pattern_query_service.get_pattern(request.ord_id)
        if row is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"pattern for ord_id={request.ord_id} not registered")
            return management_pb2.PatternAssignment()
        return pattern_to_proto(row)
