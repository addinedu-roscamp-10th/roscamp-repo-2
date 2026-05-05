# roscamp-repo-2

ROS2와 AI를 활용한 자율주행 로봇개발자 부트캠프 2팀 저장소입니다. SmartCast Robotics의 다품종 소량 생산 사형 주조 공정 스마트 팩토리 시스템을 개발합니다.

## Repository Layout

```text
ui/       User interfaces
server/   Backend services and shared database package
device/   ROS, robot, camera, and controller code
docs/     Design and test documents
scripts/  Local development scripts
```

## Server Workspace

백엔드 작업 공간은 `server/main_service/` 입니다. 현재 소스 구조는 아래와 같습니다.

```text
server/main_service/
├── src/
│   ├── interface_service/   # FastAPI HTTP API
│   └── management_service/  # Management gRPC service
└── tests/
```

공유 DB 패키지는 `server/smart_cast_db/` 에 있습니다.

### 주요 실행 명령

```bash
./scripts/run-backend.sh
./scripts/run-management.sh
./scripts/run-all.sh
```

## Test Scripts

레포 루트에서 전체 Python 테스트를 실행합니다.

```bash
./scripts/run_all_tests.sh
```

다음 경우에 전체 테스트를 권장합니다.

- PR 생성 전
- 공유 모듈 구조나 import 경로를 수정했을 때
- 두 개 이상의 서비스나 모듈을 함께 수정했을 때
- 저장소 복제 후 기본 동작을 확인할 때

작은 범위의 변경은 해당 모듈만 실행해도 됩니다.

```bash
pytest server/main_service
pytest server/ai_service
pytest ui/pyqt/factory_operator
pytest device/smartcast_arm/control
pytest device/smartcast_amr/smartcast_amr_control
pytest device/camera
```

각 Python 모듈은 보통 아래 구조를 따릅니다.

```text
module/
├── src/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── conftest.py
├── requirements.txt
└── pytest.ini
```

## Unit Test 작성 위치

각 팀원은 담당 모듈 안의 `tests/unit/` 에 unit test를 작성합니다. 실제 코드는 `src/` 에 두고, 테스트 코드는 같은 모듈의 `tests/unit/` 에 둡니다.

예를 들어 `management_service`의 `task_manager.py`를 테스트한다면:

```text
server/main_service/
├── src/management_service/
│   └── services/core/task_manager.py
└── tests/unit/
    └── test_task_manager.py
```

담당 모듈별 unit test 작성 위치는 다음과 같습니다.

```text
server/main_service/tests/unit/                         # Backend workspace
server/ai_service/tests/unit/                           # AI Service
ui/pyqt/factory_operator/tests/unit/                    # PyQt UI
device/smartcast_arm/control/tests/unit/                # Robot Arm
device/smartcast_amr/smartcast_amr_control/tests/unit/  # AMR
device/camera/tests/unit/                               # Camera
```

테스트 파일 이름은 `test_*.py` 형식으로 작성합니다.

예:

```text
test_task_manager.py
test_robot_adapter.py
test_camera_capture.py
```

자기 담당 모듈 전체 unit test를 실행하려면:

```bash
pytest server/main_service
```

특정 테스트 파일만 실행하려면:

```bash
pytest server/main_service/tests/unit/test_task_manager.py
```

여러 모듈을 수정했거나 PR을 만들기 전에는 전체 테스트를 실행합니다.

```bash
./scripts/run_all_tests.sh
```

## Shared Contracts

`management_service`에서 여러 모듈이 함께 사용하는 데이터 구조와 상태값은 아래 경로에 모여 있습니다.

```text
server/main_service/src/management_service/services/contracts/
├── models.py
├── enums.py
├── pydantic_models.py
└── protocols.py
```

예:

```python
from management_service.services.contracts.pydantic_models import CreateOrdInput
from management_service.services.contracts.enums import OrdStat, EquipStat
```

새로운 공통 데이터 구조가 필요하면 `pydantic_models.py`에 추가하고, 고정 상태값이나 코드값은 `enums.py`에 추가합니다.
