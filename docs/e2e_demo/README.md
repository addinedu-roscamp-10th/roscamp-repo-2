# SmartCast Factory e2e 시연 가이드 (우분투 PC)

> Script: `server/main_service/scripts/e2e/sim_factory_demo.py`
> 작성: 2026-05-18

가상 주문 1건을 생성하고 **MAT → TAT → 컨베이어 → 카메라 → AI 검사 → 적재** 까지의 전 흐름을 자동 실행하면서 PyQt 화면에 실시간으로 반영되도록 하는 시연 스크립트입니다.

---

## 사전 조건 (우분투 PC 측)

| 항목 | 확인 |
|---|---|
| Ubuntu 24.04 + ROS2 Jazzy 설치 | `ls /opt/ros/jazzy/setup.bash` |
| backend `.venv` + `.env.local` 셋업 | `./scripts/setup.sh` |
| MAT 로봇 (cast_python ROS2) | `ros2 node list` 에서 cast_move 등 확인 |
| TAT AMR (tat_bringup, tat_navigation) | `ros2 node list` 에서 tat_* 확인 |
| PAT 로봇 (logistics) | 동일 |
| Jetson (esp_bridge + camera) | backend `:50051` 에 PublishEvent / WatchConveyorCommands 가능 |
| ESP32 USB serial | Jetson 에 연결, 펌웨어 가동 |
| AI 서버 100.66.177.119:30000 | `curl -X POST .../predict -F file=@... -F model=EMH` → HTTP 200 |

## 가동 시작

```bash
# 1. backend (ROS2 활성)
./scripts/run-backend.sh           # :8000
./scripts/run-management.sh ros    # :50051 + ROS2

# 2. web
./scripts/run-web.sh               # :3001

# 3. PyQt
./scripts/run-pyqt.sh              # GUI

# 4. 모두 한 번에
./scripts/run-all.sh ros
```

각 서비스 가동 확인:
- `curl http://localhost:8000/health` → `{"status":"ok"}`
- `nc -z localhost 50051` → reachable
- PyQt 창에 AMR 상태 3대 표시

## 시연 실행

기본 — **사용자가 PyQt 의 ① ② ③ 버튼을 직접 누름** (영상에 실 인터랙션 보임):

```bash
./server/main_service/.venv/bin/python \
    server/main_service/scripts/e2e/sim_factory_demo.py \
    --image /path/to/conveyor_inspection.jpg \
    --cate-cd EMH \
    --phase-delay 3
```

자동 모드 — **PyQt 버튼 효과까지 스크립트가 시뮬** (사용자 인터랙션 0):

```bash
./server/main_service/.venv/bin/python \
    server/main_service/scripts/e2e/sim_factory_demo.py \
    --image /path/to/conveyor_inspection.jpg \
    --cate-cd EMH \
    --phase-delay 2 \
    --auto-buttons
```

## 옵션

| 인자 | 의미 | 기본값 |
|---|---|---|
| `--image PATH` | 검사 이미지 (컨베이어 카메라 사진) | 필수 |
| `--cate-cd CODE` | 주문 제품 카테고리 (CMH/RMH/EMH) | `EMH` |
| `--phase-delay SEC` | 각 phase 간 sleep 초 (영상 가독성) | 2 |
| `--auto-buttons` | PyQt ① ② ③ 버튼 효과를 스크립트가 자동 publish | off |
| `--keep-data` | 시연 후 DB row 보존 (재검증용) | off |
| `--schema NAME` | DB 스키마 | env `PG_SCHEMA` or `inbean` |
| `--backend URL` | backend base URL | `http://localhost:8000` |

## 10 Phase 흐름

| # | Phase | 자동/수동 |
|---|---|---|
| 0 | Preflight (backend/AI/DB/이미지 점검) | 자동 |
| 1 | 가상 주문 생성 (`/api/orders/customer`) | 자동 |
| 2 | 주문 승인 RCVD→APPR | 자동 |
| 3 | 생산 시작 (item INSERT + MM PROC) | 자동 |
| 4 | MAT chain (MM→POUR→DM) — DB polling | 자동 (실 HW 진행) |
| 5 | TAT ToPP 도착 + **레드 푸시 버튼 (① 핸드오프 ACK)** | 수동 (또는 `--auto-buttons`) |
| 6 | **RFID 스캔 (② 버튼)** | 수동 (또는 `--auto-buttons`) |
| 7 | **③ 후처리 완료** (컨베이어 1차 RUN) | 수동 (또는 `--auto-buttons`) |
| 8 | 카메라 캡처 + AI 추론 + 결과 이미지 영속화 | 자동 (실 HW 진행) |
| 9 | ToSTRG + PA_GP 적재 | 자동 (실 HW 진행) |
| 10 | 최종 검증 (4-table 일관성 + PyQt API) | 자동 |

## PyQt 화면 동기화

PyQt 가 backend API 를 1초 주기로 polling 합니다. 각 phase 진행 시:

- Phase 1~3 → 주문/생산 페이지 row 추가
- Phase 4 → 생산 상태 갱신
- Phase 5 → 핸드오프 페이지 + **RFID payload 입력란 자동 채움** (`--auto-buttons` 시 또는 RC522 스캔 시)
- Phase 6 → 후처리 옵션 페이지 갱신
- Phase 7 → 컨베이어 가동 모션
- Phase 8 → **품질 검사 페이지** 의 카메라 피드 카드에:
  - 우상단: **PASS / FAIL 배지**
  - 중앙: PatchCore 결과 이미지 (1296×308 3-패널)
  - 우하단: **검사 시각 (HH:MM:SS KST)**
- Phase 9 → 적재 상태 갱신
- Phase 10 → 검사 이력 표 row 추가

## 트러블슈팅

| 증상 | 점검 |
|---|---|
| Phase 0 backend ping 실패 | `ps aux \| grep uvicorn` 확인 |
| AI 서버 HTTP 503 | `kubectl get pods -A` 에서 model-{shape} 상태 확인 ([[project_ai_server_3_outage]]) |
| Phase 4 MAT chain timeout | `ros2 node list` 에서 cast_move 가동 확인, management 가 `run-management.sh ros` 로 띄워졌는지 |
| Phase 5 timeout (수동) | PyQt 의 ① 버튼이 EventGateway PublishEvent 가능 상태인지 확인 |
| Phase 8 timeout | Jetson camera node 가 backend `:50051` 에 연결됐는지 |
| RFID payload 자동 채움 안 됨 | `record_rfid_scan` API 또는 ESP32 RC522 펌웨어 가동 확인 |
| Phase 9 timeout | PAT 로봇 ROS2 노드 가동 확인 |

## 영상 촬영 팁

- `--phase-delay 3` 이상 권장 (phase 진행이 영상에 잘 보임)
- PyQt 화면을 한쪽 모니터, 터미널을 다른 쪽 모니터에 두면 phase 별 흐름과 PyQt 변화를 동시에 담을 수 있음
- 수동 모드 (default) 가 가장 사실적 — 실 작업자 인터랙션 시연
- 자동 모드 (`--auto-buttons`) 는 작업자 없이 시스템 흐름만 보여줄 때
