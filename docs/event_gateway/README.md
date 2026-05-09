# EventGateway — Hardware ↔ Backend EventBridge 연동 가이드

ESP32 / Jetson / PyQt 의 hardware/UI 신호와 동료 backend EventBridge 사이의
**단일 통신 채널** 셋업 가이드.

## 아키텍처

```
[ESP32]     [Jetson Python]              [Backend Python]
  │            ↓                              ↓
  │         protobuf serialize           protobuf deserialize
  │            ↓                              ↓
  │         gRPC over Tailscale  ━━━━━━━━━━>  ↓
  │                                      [EventBridge.publish]
  │                                          │
  │                                  ┌───────┴────────┐
  │                                  ↓                ↓
  └─ Jetson 측은 stub 만 다룸     state_manager   AI server adapter
                                  monitor_agent   ...
```

- **wire 계약**: `proto/event_gateway.proto`
- **EventType 카탈로그**: `core/events.py` (StrEnum) — 양쪽이 같은 string 사용
- **publish 측** (sender): Jetson `event_gateway_client.py` + PyQt `monitoring/app/clients/event_gateway.py`
- **subscribe 측** (receiver): Jetson `esp_bridge._setup_event_gateway_subscribe` (INSP_COMPLETED → ESP32 RUN)

## 메시지 표

### Jetson → Backend (publish)

| EventType | Trigger | Payload |
|---|---|---|
| `HANDOFF_ACK` | 하드웨어 GPIO33 핸드오프 버튼 / PyQt ① 핸드오프 ACK 버튼 | `{zone, source_device, button}` |
| `RFID_SCANNED` | RC522 NDEF Text 스캔 | `{reader_id, zone, raw_payload, uid, source_kind}` |
| `TOF1_ENTRY` | 카메라 앞 정지 센서 ON edge | `{sensor_id, on, raw_mm}` |
| `PP_DONE_REQUESTED` | PyQt ③ 후처리 완료 버튼 (3초 카운트다운 후) | `{source_device, button, item_id, rfid_payload}` |

### Backend → Jetson (subscribe)

| EventType | 발행 주체 | Jetson 처리 |
|---|---|---|
| `INSP_COMPLETED` | 검사 완료 시 backend (state_manager / AI adapter) | `esp_bridge.send_command("start")` 자동 dispatch (출구 방향 컨베이어 ON) |

## 동료 backend 셋업 단계

### 1. proto 컴파일

```bash
# repo root 에서
cd <백엔드 venv>
pip install grpcio-tools

# server 측 generated 디렉토리에 컴파일
mkdir -p server/main_service/generated
python -m grpc_tools.protoc \
  -I proto \
  --python_out=server/main_service/generated \
  --pyi_out=server/main_service/generated \
  --grpc_python_out=server/main_service/generated \
  proto/event_gateway.proto

# 또는 자동화 — repo root 에서
bash scripts/setup-event-gateway-backend.sh
```

### 2. EventType StrEnum 추가

기존 `core/events.py` (또는 동료 backend 의 EventType 카탈로그) 에 다음 추가:

```python
class EventType(StrEnum):
    # 기존 ...
    HANDOFF_ACK = "HANDOFF_ACK"
    RFID_SCANNED = "RFID_SCANNED"
    TOF1_ENTRY = "TOF1_ENTRY"
    # 추가 (2026-05-08)
    PP_DONE_REQUESTED = "PP_DONE_REQUESTED"   # PyQt ③ 후처리 완료 버튼
    INSP_COMPLETED = "INSP_COMPLETED"         # 검사 완료 — Jetson 자동 RUN
```

### 3. EventGatewayServicer 등록

`docs/event_gateway/event_gateway_servicer.py.template` 참고하여 backend gRPC server 에 servicer 등록:

```python
# server build 시
from generated import event_gateway_pb2_grpc as eg_pb_grpc
from your_backend.event_gateway import EventGatewayServicer

eg_pb_grpc.add_EventGatewayServicer_to_server(
    EventGatewayServicer(bridge=your_event_bridge_instance),
    grpc_server,
)
```

`EventGatewayServicer` 의 핵심:
- `PublishEvent(unary)` — EventEnvelope → Pydantic Event 변환 + EventBridge.publish + idempotency dedup TTL 60s
- `WatchEvents(server-streaming)` — EventBridge.subscribe 등록 + queue → stream + cancel 시 unsubscribe

### 4. INSP_COMPLETED 발행 시점

검사 완료 시 backend 가 다음 publish:

```python
event_bridge.publish(Event(
    event_type=EventType.INSP_COMPLETED,
    item_id=item_id,                       # 검사한 item PK
    txn_id=insp_task_txn_id,               # 검사 task txn (선택)
    payload={
        "result": True,                    # pass/fail (선택)
        "captured_image_path": "...",      # (선택)
    },
))
```

→ Jetson 의 WatchEvents stream 이 받음 → `esp_bridge.send_command("start")` 자동 dispatch
→ ESP32 모터 ON → 펌웨어 1.7.0 RUN_DURATION_MS=4000 timer 후 자동 STOPPED → 출구 AMR 핸드오프 트리거.

## Jetson / PyQt 측 환경변수

```bash
# Jetson ~/casting-image-publisher/env
EVENT_GATEWAY_TARGET=<backend_host>:<port>     # 예: 100.87.158.76:50051
EVENT_GATEWAY_ENABLED=1                         # 0 시 silent skip (기존 RPC 흐름 유지)
EVENT_GATEWAY_SOURCE=jetson-orin-nx-01         # 선택, default hostname

# PyQt 환경변수 (.env 또는 export)
EVENT_GATEWAY_TARGET=<backend_host>:<port>
```

## 검증 명령

### 동료 backend 측

```bash
# servicer 등록 확인 (gRPC reflection 활성 시)
grpcurl -plaintext <host>:<port> list
# casting.management.v2.event.EventGateway 가 보여야

# 수동 publish test
grpcurl -plaintext -d '{"event":{"event_type":"HANDOFF_ACK","resource_id":"CONV1","source":"manual"}}' \
  <host>:<port> casting.management.v2.event.EventGateway/PublishEvent

# WatchEvents test
grpcurl -plaintext -d '{"event_types":["INSP_COMPLETED"],"consumer":"manual"}' \
  <host>:<port> casting.management.v2.event.EventGateway/WatchEvents
```

### Jetson 측

```bash
ssh jetson@<jetson-host> "journalctl -u casting-image-publisher.service -f | grep event_gateway"

# 활성화 시 다음 라인 보임
# EventGatewayClient 시작: target=<host>:<port> source=jetson-...
# [event_gateway] INSP_COMPLETED subscribe 등록 — Jetson 자동 ESP32 RUN
# EventGatewayClient WatchEvents 시작 event_types=['INSP_COMPLETED']
```

## 주의 사항

- **Event.resource_id VARCHAR(10) 제약**: `reader_id="RFID-CONV-01"` 등 11자 이상은 servicer 의 length 가드가 payload 로 demote (구현 참고: template 의 `_envelope_to_event`).
- **Idempotency**: 동일 `idempotency_key` 60초 내 재진입 시 `deduplicated=true` 응답 + EventBridge.publish 생략. hardware 재전송 안전.
- **Slow consumer**: WatchEvents queue maxsize 1024 — 가득 차면 oldest drop (구현 참고: template).
- **보안**: 현재 insecure gRPC. mTLS 는 후속 SPEC.

## Reference

- proto: `proto/event_gateway.proto`
- Servicer template: `docs/event_gateway/event_gateway_servicer.py.template`
- Setup script: `scripts/setup-event-gateway-backend.sh`
- Jetson client: `device/camera/src/camera/jetson_publisher/event_gateway_client.py`
- Jetson hooks: `device/camera/src/camera/jetson_publisher/esp_bridge.py` (검색: `_publish_via_event_gateway`, `_setup_event_gateway_subscribe`)
