# SmartCastRobotics — 설치 및 실행 가이드

다른 PC에서 처음 clone 후 모든 서비스를 띄우는 절차입니다.

## 빠른 시작

| OS | 설치 | 실행 | 중단 |
|----|------|------|------|
| Ubuntu 24.04 | `./scripts/setup.sh` | `./scripts/run-all.sh` | `./scripts/stop-all.sh` |
| Windows | `scripts\setup.bat` | `scripts\run-all.bat` | `scripts\stop-all.bat` |

설치 후 **`server/main_service/.env.local`의 `DATABASE_URL`** 비밀번호와 RDS 엔드포인트만 채우면 됩니다. 값은 별도로 받아서 입력하세요.

개별 실행 스크립트:
- `run-backend.sh` / `.bat` - FastAPI `:8000`
- `run-management.sh` - Management gRPC `:50051`
- `run-pyqt.sh` / `.bat` - PyQt Monitoring 데스크톱
- `run-web.sh` / `.bat` - Next.js `:3001`

`run-all.sh`는 GUI 터미널(`gnome-terminal`, `konsole`, `xfce4-terminal`, `mate-terminal`, `lxterminal`, `xterm`)이 있으면 `backend / management / pyqt / web`을 각각 별도 창으로 실행합니다.
GUI 터미널을 열 수 없으면 `logs/{backend,management,pyqt,web}.log`로 백그라운드 실행합니다.
`run-all.bat`는 별도 cmd 창을 띄웁니다.

---

## 수동 절차

## 1. 사전 요구사항

| 항목 | 권장 버전 | 용도 |
|------|----------|------|
| Python | 3.11 (backend) / 3.12 (PyQt) | 가상환경 분리 |
| Node.js | 20+ | Next.js (ui/web) |
| PostgreSQL 클라이언트 | libpq-dev | psycopg 빌드 (`sudo apt install -y libpq-dev` on Ubuntu 24.04) |
| AWS RDS 인증서 | global-bundle.pem | `sslmode=verify-full` 용 (https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem) |
| Git | any | clone |

Ubuntu 24.04에서는 다음 패키지를 먼저 설치하면 편합니다.

```bash
sudo apt update
sudo apt install -y build-essential libpq-dev python3-pip python3-venv python3.12 python3.12-venv
```

backend 용 `python3.11` 이 별도로 필요하면, 시스템에 이미 설치되어 있는지 확인한 뒤 사용하세요.

## 2. 저장소 가져오기

```bash
git clone https://github.com/addinedu-roscamp-10th/roscamp-repo-2.git
cd roscamp-repo-2
```

## 3. Backend workspace (`server/main_service/`) - FastAPI `:8000` + Management gRPC `:50051`

현재 백엔드 작업 공간은 `server/main_service/`이고, 소스 패키지는 `src/interface_service/`와 `src/management_service/`입니다.

### 3.1 가상환경 + 의존성

```bash
cd server/main_service
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3.2 환경변수 (`.env.local`)

```bash
cp .env.example .env.local
```

`.env.local`을 열어 실제 값으로 교체합니다.

```env
DATABASE_URL="postgresql+psycopg://postgres:<PASSWORD>@<RDS_ENDPOINT>:5432/Casting?sslmode=verify-full&sslrootcert=/absolute/path/to/global-bundle.pem"
SMARTCAST_SCHEMA=public
```

비밀번호와 RDS 엔드포인트는 별도로 받아서 입력하세요. 보안상 git에 커밋하지 않습니다.

### 3.3 실행

FastAPI 실행:

```bash
PYTHONPATH=src/interface_service:src/management_service:src \
uvicorn app.main:app --host 0.0.0.0 --port 8000 --env-file .env.local --reload
```

확인: `curl http://localhost:8000/api/dashboard/stats` → 200 응답

Management gRPC 실행:

```bash
PYTHONPATH=src/management_service:src/interface_service:src \
python src/management_service/server.py
```

확인: `:50051` LISTEN

## 4. PyQt Monitoring (`ui/pyqt/factory_operator/`) - 데스크톱 앱

```bash
cd ui/pyqt/factory_operator
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m factory_operator.main
```

`API_BASE_URL` 환경변수로 백엔드 주소 변경이 가능합니다. 기본값은 `http://localhost:8000`입니다.
`CASTING_DATA_MODE=fallback`을 쓰면 백엔드 미기동 시 mock 데이터를 사용합니다.

## 5. Web (`ui/web/`) - Next.js `:3001`

```bash
cd ui/web
npm install
npm run dev -- --port 3001
```

브라우저: http://localhost:3001

`NEXT_PUBLIC_API_BASE_URL` 등 백엔드 주소 환경변수가 필요하면 `.env.local`을 작성합니다. 프로젝트 기본값은 `http://localhost:8000`입니다.

## 6. 통합 동작 확인

| 서비스 | 확인 |
|--------|------|
| FastAPI | `curl http://localhost:8000/api/orders` → 200, JSON 배열 |
| Management gRPC | `run-management.sh` 실행 후 50051 수신 |
| PyQt | 데스크톱 창 표시 + 실시간 운영 모니터링 페이지 발주 목록 |
| Web | http://localhost:3001 admin 대시보드 정상 |

## 7. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `psycopg.OperationalError: connection refused` | `DATABASE_URL` 또는 RDS 인증서 경로 오류 | `.env.local`의 `sslrootcert=` 절대 경로 확인 |
| `psycopg.errors.UndefinedTable` | 잘못된 스키마 | `SMARTCAST_SCHEMA=public` 명시 |
| `ModuleNotFoundError: app` | `PYTHONPATH` 또는 현재 작업 디렉터리 오류 | `server/main_service/`에서 실행하고 `src/interface_service:src/management_service:src`를 포함 |
| `ModuleNotFoundError: management_service` | `PYTHONPATH` 미지정 | Management 실행 시 `src/management_service`를 포함 |
| `ModuleNotFoundError: smart_cast_db` | `server/` 경로가 sys.path에 없음 | `server/main_service/`에서 실행하고 `src`를 포함 |
| PyQt가 백엔드 응답을 못함 | 포트/방화벽 | `lsof -i :8000`으로 LISTEN 확인 |
| Next.js dev가 다른 포트 점유 | 좀비 프로세스 | `lsof -i :3001` → `kill <PID>` 후 재시작 |

## 8. 운영 메모

- 활성 DB: **AWS RDS Casting** (`public` schema, 약 33개 테이블)
- 공통 계약 데이터 구조와 Enum은 `server/main_service/src/management_service/services/contracts/`에 둡니다.
- `management_service`의 런타임 진입점은 `server/main_service/src/management_service/server.py`입니다.
- e2e 검증 스크립트는 `server/main_service/scripts/e2e/`에 있습니다.
