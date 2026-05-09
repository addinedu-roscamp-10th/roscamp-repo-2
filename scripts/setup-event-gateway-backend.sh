#!/usr/bin/env bash
# EventGateway backend 셋업 자동화 — 동료 backend PC 에서 1회 실행.
#
# 작업:
#   1. proto 컴파일 → server 측 generated 디렉토리에 *_pb2.py / *_pb2_grpc.py 생성
#   2. 셋업 가이드 출력 (servicer 등록 + EventType 추가 단계)
#
# 사용:
#   bash scripts/setup-event-gateway-backend.sh
#   bash scripts/setup-event-gateway-backend.sh --out server/main_service/generated
#
# 사전:
#   - 동료 backend Python venv 활성화 (grpcio-tools 포함)
#   - repo root 에서 실행

set -euo pipefail

# ----------------------------------------------------------------------------
# 옵션 파싱
# ----------------------------------------------------------------------------
OUT_DIR="server/main_service/generated"
PROTO_FILE="proto/event_gateway.proto"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)
      OUT_DIR="$2"
      shift 2
      ;;
    --proto)
      PROTO_FILE="$2"
      shift 2
      ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# //'
      exit 0
      ;;
    *)
      echo "ERROR: unknown option $1" >&2
      exit 1
      ;;
  esac
done

# ----------------------------------------------------------------------------
# 검증
# ----------------------------------------------------------------------------
if [[ ! -f "$PROTO_FILE" ]]; then
  echo "ERROR: proto file not found: $PROTO_FILE" >&2
  echo "       repo root 에서 실행했는지 확인하세요." >&2
  exit 2
fi

if ! python -c 'import grpc_tools' 2>/dev/null; then
  echo "ERROR: grpc_tools 미설치 — pip install grpcio-tools 필요" >&2
  exit 3
fi

# ----------------------------------------------------------------------------
# 컴파일
# ----------------------------------------------------------------------------
mkdir -p "$OUT_DIR"
echo ">>> 컴파일 중: $PROTO_FILE → $OUT_DIR/"

python -m grpc_tools.protoc \
  -I "$(dirname "$PROTO_FILE")" \
  --python_out="$OUT_DIR" \
  --pyi_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$PROTO_FILE"

# generated/__init__.py 생성 (없으면)
[[ -f "$OUT_DIR/__init__.py" ]] || touch "$OUT_DIR/__init__.py"

echo ">>> 컴파일 완료:"
ls -1 "$OUT_DIR"/event_gateway_pb2*

# ----------------------------------------------------------------------------
# 다음 단계 가이드
# ----------------------------------------------------------------------------
cat <<EOF

==============================================================================
다음 단계 — 동료 backend 코드 작업
==============================================================================

(1) EventType StrEnum 에 신규 항목 2개 추가
    파일: <core/events.py 또는 동료의 EventType 카탈로그>

    PP_DONE_REQUESTED = "PP_DONE_REQUESTED"   # PyQt ③ 후처리 완료 버튼
    INSP_COMPLETED    = "INSP_COMPLETED"      # 검사 완료 — Jetson 자동 RUN

(2) EventGatewayServicer 구현 — template 참고
    참고 파일: docs/event_gateway/event_gateway_servicer.py.template
    배치 위치: <동료 backend 의 service 디렉토리>/event_gateway.py

(3) gRPC server build 시 servicer 등록

    from $OUT_DIR import event_gateway_pb2_grpc as eg_pb_grpc
    from <your_backend>.event_gateway import EventGatewayServicer

    eg_pb_grpc.add_EventGatewayServicer_to_server(
        EventGatewayServicer(bridge=your_event_bridge_instance),
        grpc_server,
    )

(4) 검증 명령 (gRPC reflection 활성 시)

    grpcurl -plaintext <host>:<port> list
    # casting.management.v2.event.EventGateway 가 보여야 함

    grpcurl -plaintext \\
      -d '{"event":{"event_type":"HANDOFF_ACK","resource_id":"CONV1","source":"manual"}}' \\
      <host>:<port> casting.management.v2.event.EventGateway/PublishEvent

자세한 가이드: docs/event_gateway/README.md
==============================================================================
EOF
