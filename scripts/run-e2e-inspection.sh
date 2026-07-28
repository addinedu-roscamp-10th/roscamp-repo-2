#!/usr/bin/env bash
# E2E: 🔴 빨간 푸시 버튼 → 검사 이미지 캡처 → AI 추론 → DB 저장 한 사이클 검증.
#
# 사전 조건:
#   1. PR #9 (EventGateway + UploadInspectionImage RPC) dev 머지 완료
#   2. AI inspection chain PR (본 PR) 머지 완료
#   3. HW 모드: ESP32 펌웨어 v1.7.0 + USB Serial 연결 + Jetson 에서
#              casting-image-publisher systemd 활성 (EventGateway client + camera)
#   4. main_service/.env.local + .venv (httpx 포함) — scripts/setup.sh
#   5. AWS RDS smartcast 스키마 접속 가능 + 가용 item 1 개 이상
#   6. (HW 모드 전제) 별도 터미널에서 `bash scripts/run-management.sh ros` 실행 중
#
# 사용:
#   bash scripts/run-e2e-inspection.sh            # HW 모드 (실 버튼 push 대기)
#   bash scripts/run-e2e-inspection.sh --sim      # SIM 모드 (HW 없이 backend 만 가동)
#   bash scripts/run-e2e-inspection.sh --keep     # 검증 후 테스트 row 보존
#   E2E_TIMEOUT_SEC=300 bash scripts/run-e2e-inspection.sh   # 대기 시간 조절
#
# 흐름:
#   ① pre-flight (DB / .env.local / psql)
#   ② Mock AI 서버 시동 (포트 8100, background)
#   ③ Backend gRPC :50051 활성 확인 (HW 모드)
#   ④ DB 모니터링 + 결과 검증 (Python 스크립트 위임)
#   ⑤ 종료 시 Mock AI 자동 정리 + (옵션) 테스트 row cleanup

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="hw"
KEEP_DATA=""
for arg in "$@"; do
    case "$arg" in
        --sim) MODE="sim" ;;
        --hw)  MODE="hw" ;;
        --keep) KEEP_DATA="--keep-data" ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *) echo "✗ 알 수 없는 인자: $arg (--sim | --hw | --keep)" ; exit 1 ;;
    esac
done

TIMEOUT_SEC="${E2E_TIMEOUT_SEC:-180}"

# .env.local 자동 로드 (DATABASE_URL, MGMT_AI_HOST 등)
ENV_FILE="$ROOT/server/main_service/.env.local"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
else
    echo "⚠ $ENV_FILE 없음 — 환경변수가 이미 export 되어 있어야 합니다."
fi

echo ""
echo "================================================================"
echo "  🔴 E2E Inspection Flow Test  (mode=$MODE)"
echo "  push button → ESP32 → Jetson → backend → AI mock → DB"
echo "================================================================"
echo ""

# ---------- pre-flight ----------
echo "[1/5] Pre-flight 검사..."
[ -n "${DATABASE_URL:-}" ] \
    || { echo "✗ DATABASE_URL 미설정 — .env.local 또는 export 필요"; exit 1; }
command -v psql >/dev/null \
    || { echo "✗ psql CLI 미설치 (DB sanity check 용) — brew install libpq 권장"; exit 1; }

# postgresql+psycopg:// → postgresql:// (psql 호환)
PG_PSQL_URL="$(echo "$DATABASE_URL" | sed -E 's|postgresql\+psycopg(2?)://|postgresql://|' )"
if ! psql "$PG_PSQL_URL" -c 'SELECT 1' >/dev/null 2>&1; then
    echo "✗ RDS 연결 실패 — DATABASE_URL 확인"
    exit 1
fi
echo "    RDS 연결 OK"

PYTHON="$ROOT/server/main_service/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "✗ main_service/.venv 없음 — scripts/setup.sh 먼저 실행"
    exit 1
fi

# ---------- start mock AI ----------
echo "[2/5] Mock AI 서버 시동..."
mkdir -p "$ROOT/logs"
AI_LOG="$ROOT/logs/e2e-ai-mock.log"
nohup bash "$ROOT/scripts/run-ai-mock.sh" > "$AI_LOG" 2>&1 &
AI_PID=$!

cleanup() {
    local rc=$?
    echo ""
    echo "[cleanup] Mock AI 서버 종료 (pid=$AI_PID)..."
    kill "$AI_PID" 2>/dev/null || true
    wait "$AI_PID" 2>/dev/null || true
    exit $rc
}
trap cleanup EXIT INT TERM

for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8100/health >/dev/null 2>&1; then
        echo "    Mock AI ready  (http://127.0.0.1:8100  pid=$AI_PID  log=$AI_LOG)"
        break
    fi
    sleep 0.5
done
if ! curl -fsS http://127.0.0.1:8100/health >/dev/null 2>&1; then
    echo "✗ Mock AI 시동 실패. tail -n 30 $AI_LOG"
    tail -n 30 "$AI_LOG" || true
    exit 1
fi

# ---------- check backend (HW only) ----------
if [ "$MODE" = "hw" ]; then
    echo "[3/5] Backend gRPC :${MANAGEMENT_GRPC_PORT:-50051} 활성 확인..."
    MGMT_PORT="${MANAGEMENT_GRPC_PORT:-50051}"
    if command -v nc >/dev/null 2>&1 && nc -z 127.0.0.1 "$MGMT_PORT" 2>/dev/null; then
        echo "    Backend :$MGMT_PORT 활성"
    else
        echo "    ⚠ Backend :$MGMT_PORT 미응답 — 별도 터미널에서 backend 를 먼저 띄우세요:"
        echo "         bash scripts/run-management.sh ros"
        echo "    (HW 모드는 backend EventGateway 가 Jetson HANDOFF_ACK 을 받아야 동작합니다)"
        exit 1
    fi
else
    echo "[3/5] SIM 모드 — backend gRPC 미사용, Python 인-프로세스 사슬 직접 호출"
fi

# ---------- run watcher / simulator ----------
echo "[4/5] DB 변화 모니터링 + 검증 (timeout=${TIMEOUT_SEC}s)"
if [ "$MODE" = "hw" ]; then
    echo ""
    echo "    🔴 빨간 푸시 버튼을 눌러주세요"
    echo "       (또는 Jetson 에서: ssh jetson 'echo sim_ack | sudo tee /dev/ttyUSB0')"
    echo ""
fi

E2E_PY="$ROOT/server/main_service/scripts/e2e/watch_inspection_flow.py"
PYTHONPATH="$ROOT/server/main_service/src/management_service:$ROOT/server/main_service/src:$ROOT/server" \
    MGMT_AI_HOST=127.0.0.1 \
    MGMT_AI_PORT=8100 \
    MGMT_INSP_IMAGE_SAVE_DIR="${MGMT_INSP_IMAGE_SAVE_DIR:-/tmp/e2e_inspections}" \
    "$PYTHON" "$E2E_PY" --mode "$MODE" --timeout "$TIMEOUT_SEC" $KEEP_DATA

# ---------- summary ----------
echo ""
echo "[5/5] ✅ E2E 완료"
