# 🧑‍🏭 SmartCast Robotics
> **주문 접수부터 생산, 품질 검사, 적재 및 출하까지 전체 공정을 통합 관리하는 스마트팩토리 관제 시스템**

**📄 주문 관리**: Web을 통한 주문 접수 및 관리자 승인<br>
**🖥️ 생산 관제**: PyQt를 통한 생산 시작, 모니터링<br>
**🏭 장비 연동**: AMR, 로봇팔, Conveyor 및 AI 검사 서버 연동<br>
**🔄 공정 자동화**: 주문 → 생산 → 품질 검사 → 적재 → 출하 흐름 제어<br>

## 목차

- [Demo](#demo)
- [System Overview](#system-overview)
- [How It Works](#how-it-works)
- [Folder Structure](#folder-structure)
- [Getting Started](#getting-started)

## Demo

### UI
| Web (사용자, 관리자) | PyQt (공장 작업자) |
| --- | --- |
| <img width="380" alt="Web" src="https://github.com/user-attachments/assets/a1401470-608a-45ae-93f3-74283adc524e" /> | <img width="380" alt="PyQt" src="https://github.com/user-attachments/assets/6c1815b1-1873-498c-81c6-0ba44c1c6d84" /> |

### Full Process
https://github.com/user-attachments/assets/6156e17b-4034-45b5-9016-b879d27fd837


## System Overview

### Software Architecture

<img width="1001" height="574" alt="Architecture" src="https://github.com/user-attachments/assets/cbdc1ff9-8545-4b99-838f-3ce2529ab3d2" />

### 주요 서비스

<table>
  <thead>
    <tr>
      <th width="18%">서비스</th>
      <th width="34%">역할</th>
      <th width="22%">통신 방식</th>
      <th width="26%">주요 기술</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Interface Service</td>
      <td>Web UI 요청 처리, 주문 및 생산 정보 조회·관리</td>
      <td>REST API, gRPC, SQL</td>
      <td>FastAPI</td>
    </tr>
    <tr>
      <td>Management Service</td>
      <td>생산 흐름 제어, 공정 상태 관리, 장비 연동</td>
      <td>REST API, gRPC, SQL, ROS2</td>
      <td>ROS2 (Zenoh Bridge), asyncio</td>
    </tr>
    <tr>
      <td>Web UI</td>
      <td>사용자 주문 웹과 관리자 웹</td>
      <td>REST API</td>
      <td>Next.js</td>
    </tr>
    <tr>
      <td>Factory Operator UI</td>
      <td>작업자용 데스크톱 애플리케이션</td>
      <td>gRPC</td>
      <td>PyQt</td>
    </tr>
    <tr>
      <td>AI Service</td>
      <td>제품 결함 검사</td>
      <td>REST API</td>
      <td>FastAPI, Kubernetes, GroundedSAM2, PatchCore</td>
    </tr>
    <tr>
      <td>Database</td>
      <td>주문, 생산, 작업, 장비 상태와 검사 정보 저장</td>
      <td>SQL</td>
      <td>PostgreSQL, SQLAlchemy AsyncIO, psycopg3 </td>
    </tr>
  </tbody>
</table>

### 주요 장비

| 장비 및 구성요소 | 역할 | 통신 방식 | 제어 장치 |
| --- | --- | --- | --- |
| AMR(TAT)  | 공정 간 제품 운반 | ROS2 | Raspberry Pi 4 |
| 로봇팔(MAT, PAT)  | 생산(MAT), 적재·출고 작업 수행(PAT) | ROS2 | Raspberry Pi 5 |
| Camera | 검사 이미지 촬영과 업로드 | gRPC | Jetson Orin NX |
| Conveyor | 제품 이송 | USB Serial | ESP32 |
| Handoff Button | 후처리 구역의 인계 완료 입력 | GPIO | ESP32 |
| RFID Reader | 제품 RFID 태그 인식 | SPI | ESP32 |
| TOF Sensor | 카메라 앞 제품 도착 감지 | UART | ESP32 |

### Map

| 실제 공정 배치 | Occupancy Grid Map |
| --- | --- |
| <img width="882" height="504" alt="map_layout" src="https://github.com/user-attachments/assets/36d58250-c531-4ae6-878c-44579ea4d575" /> | <img width="915" height="503" alt="map" src="https://github.com/user-attachments/assets/1adeef5f-82c1-49d7-aa94-51296f579669" />|

### ERD & AI Inspection

| ERD | AI Inspection |
| --- | --- |
| <img width="1655" height="804" alt="ERD" src="https://github.com/user-attachments/assets/759c362c-2bdd-433e-9941-33fde37addb2" />| <img width="2041" height="770" alt="AI_inspection_result" src="https://github.com/user-attachments/assets/ae44a863-d7e9-4561-a4a2-acebda0cb613" />|

## How It Works

### 주문 접수 및 승인

```text
[Web] 사용자 주문 입력 및 관리자 주문 승인
      │ REST API
      ▼
[Interface Service]
      │ 주문 상태 DB 반영
      ▼
[AWS RDS (PostgreSQL)]
```

### 생산 시작 및 공정 제어

```text
[PyQt] 승인 주문 조회 및 생산 시작 요청
      │ gRPC
      ▼
[Management Service]
      ├─ 주문 조회 → AWS RDS (PostgreSQL)
      └─ 생산 시작 → Orchestrator
             ├─ AWS RDS (PostgreSQL)에 진행 상태 반영
             └─ 작업 생성, 자원 배정, 상태 전이, 실행 순서 조정
                    │
                    ▼
                 Adapter
                    ├─ AMR(ROS2)
                    ├─ 로봇팔(ROS2)
                    ├─ Conveyor(gRPC → Jetson → USB Serial → ESP32)
                    └─ AI Server(REST API)
                              │
                              ▼
                    장비 작업 수행 및 실행 결과 반환
```


### 공정 처리 단계

#### 1. 주문 접수 및 생산 시작

* **사용자**: 웹을 통한 주문 등록
* **관리자**: 주문 내용과 생산 가능 여부 확인 후 승인
* **작업자**: PyQt에서 주문을 선택하고 Management Service에 생산 시작 요청

#### 2. 생산 작업 생성 및 공정 제어
Orchestrator는 생산 시작 요청과 작업 완료 이벤트를 받아 각 모듈의 실행 순서를 조정합니다.

| 모듈 | 역할 |
| --- | --- |
| Orchestrator | 생산 시작 요청과 작업 완료 이벤트를 받아 후속 작업 흐름 조정 |
| Task Manager | 품목 상태에 따라 다음 작업을 생성하고 적재 슬롯 예약과 출고 계획 관리 |
| Task Allocator | 작업 유형, 장비 상태와 가용 여부를 바탕으로 실행 장비 결정 |
| Task Executor | 작업을 장비별 실행 단계로 나누어 Adapter를 호출하고 실행 결과 반영 |

#### 3. 작업 실행 명령 전달
장비별 **Adapter**를 통해 작업 명령을 각 장비에 맞는 방식으로 전달합니다.

* **로봇팔 및 AMR**: Zenoh Bridge 기반 ROS2 Action 호출
* **컨베이어**: gRPC로 Jetson에 명령 전달 → USB Serial로 ESP32 제어
* **AI Service**: REST API를 통한 품질 검사 요청

#### 4. 실행 결과 반영
* **결과 수신**: 각 Adapter에서 ROS2 Action 결과와 AI Service 응답 수신
* **상태 갱신**: 수신 결과를 바탕으로 작업 및 공정 상태 갱신

#### 공정 이벤트 반영
* **장비 이벤트**: 하차 완료 버튼 입력과 RFID 인식 결과를 EventGateway를 통해 Management Service에 전달
* **검사 이미지**: 검사 대상 원본 이미지를 gRPC를 통해 Management Service에 업로드

#### 데이터 저장
**State Manager**는 주문, 품목, 작업과 장비 상태를 조회하고 메모리와 데이터베이스의 상태 변경을 처리합니다.
* **PostgreSQL**: 주문, 품목, 작업, 공정, 장비 상태 및 불량 검사 정보
* **Management Service 서버**: 검사 원본 이미지 및 결과 이미지

## Folder Structure

```text
roscamp-repo-2/
├── device/
│   ├── camera/
│   ├── conveyor_controller/
│   ├── smartcast_amr/
│   └── smartcast_arm/
├── server/
│   ├── ai_service/      불량탐지 AI Service
│   ├── main_service/    Interface Service, Management Service
│   └── smart_cast_db/   공통 DB 모듈
├── ui/
│   ├── pyqt/   PyQt 작업자 화면
│   └── web/    Next.js 관리자 웹, 사용자 주문 웹
├── zenoh/    Management Service와 ROS2 장비 간 Zenoh Bridge 설정
├── proto/    UI, 장비와 Management Service 간 gRPC 계약
└── scripts/  환경 설정, 실행 스크립트
```

## Getting Started

### 요구 사항

- Ubuntu 24.04
- Python 3.12, `venv`, `pip`
- Node.js 20 이상과 npm
- 로컬 PostgreSQL 또는 접근 가능한 AWS RDS 인스턴스
- 장비 연동 시: ROS2 Jazzy, cast_python(`device/smartcast_arm/mat`) 오버레이, `rmw_cyclonedds_cpp`, `zenoh-bridge-ros2dds`

### 초기 설정

1. 저장소를 복제합니다.

   ```bash
   cd ~
   git clone https://github.com/addinedu-roscamp-10th/roscamp-repo-2.git
   cd roscamp-repo-2
   ```

2. Python 시스템 패키지, Main Service와 PyQt 가상환경, Web 의존성을 설치합니다.

   ```bash
   ./scripts/setup.sh
   ```

3. `server/main_service/.env.local`에서 데이터베이스 URL과 스키마를 설정합니다.

### Server

로컬 UI와 서버를 함께 실행합니다.

```bash
./scripts/run-all.sh       # Interface Service, Management Service, PyQt, Web UI 실행
./scripts/run-all.sh ros   # ROS2 모드와 Management Zenoh Bridge 실행
```

> ROS2 모드는 Domain ID `100`을 사용하며 Management Zenoh Bridge는 `tcp/0.0.0.0:7447`에서 연결을 대기합니다.

UI와 서버를 종료합니다.

```bash
./scripts/stop-all.sh
```

### Robots

1. 각 로봇에서 ROS2 패키지를 빌드하고 환경을 설정한 뒤, Bringup 또는 Action Server를 실행합니다.
2. 로봇별 Bridge 설정에서 아래 값을 채웁니다.

   - `domain`: 로봇 ROS2 Domain ID
   - `namespace`: 실제 로봇 namespace, 예: `/TAT1`, `/MAT1`, `/PAT1`
   - `connect.endpoints`: Management 서버의 `tcp/<MANAGEMENT_HOST_IP>:7447`

3. Management Bridge를 먼저 시작한 뒤, 로봇별 Bridge를 실행합니다.

| 장비 | 설정 파일 | 실행 명령 |
| --- | --- | --- |
| TAT(AMR) | `device/smartcast_amr/zenoh/bridge_TAT.json5` | `./device/smartcast_amr/zenoh/run-zenoh-bridge.sh` |
| MAT(생산 로봇팔) | `device/smartcast_arm/mat/zenoh/bridge_MAT.json5` | `./device/smartcast_arm/mat/zenoh/run-zenoh-bridge.sh` |
| PAT(적재·출고 로봇팔) | `device/smartcast_arm/pat/zenoh/bridge_PAT.json5` | `./device/smartcast_arm/pat/zenoh/run-zenoh-bridge.sh` |

### AI Server

AI Server의 구성과 실행 방법은
[`server/ai_service/README.md`](server/ai_service/README.md)를 참고합니다.

### 개별 실행

```bash
./scripts/run-backend.sh      # FastAPI, :8000
./scripts/run-management.sh   # Management gRPC, :50051
./scripts/run-pyqt.sh         # PyQt 작업자 UI
./scripts/run-web.sh          # Next.js, :3001
```
