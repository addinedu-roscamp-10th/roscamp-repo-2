"""Pattern-related Management gRPC methods."""

from __future__ import annotations

import grpc
import management_pb2  # type: ignore

from rpc.proto_helpers import pattern_to_proto


class PatternRpcMixin:
    """Pattern read/write RPCs."""

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

    def RegisterPattern(self, request, context):
        try:
            row = self.pattern_command_service.register_pattern(
                ord_id=request.ord_id,
                ptn_loc_id=request.ptn_loc_id,
            )
        except ValueError as exc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(exc))
            return management_pb2.PatternAssignment()
        except LookupError as exc:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(exc))
            return management_pb2.PatternAssignment()
        return pattern_to_proto(row)
