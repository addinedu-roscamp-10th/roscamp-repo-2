#!/usr/bin/env bash
# HW 풀 e2e — Ubuntu 백엔드 개발자가 dev 머지 직후 RA + TAT + CONV1 전체 사슬 검증용 배치.
#
# 사전 조건:
#   - Ubuntu 24.04 + scripts/setup.sh 1회 실행 완료 (.venv, .env.local 준비됨)
#   - ROS2 jazzy 설치 (/opt/ros/jazzy/setup.bash 또는 MGMT_ROS2_SETUP 환경변수)
#   - Tailscale + Jetson(`MGMT_JETSON_HOST`) SSH key 등록 — 옵션 (--skip-jetson 으로 우회 가능)
#   - 실 RA 암(ROS2), TAT AMR(ROS2), CONV1(ESP32 펌웨어 v1.7.0+) 가 라인에 연결되어 있을 것
#   - AWS RDS `Casting` 스키마 접속 가능 (DATABASE_URL)
#
# 동작:
#   1) 사전 점검 — .env.local / .venv / RDS / ROS2 setup / Jetson SSH
#   2) Jetson `casting-image-publisher.service` 활성 확인 (필요 시 재시작)
#   3) 4 서비스 백그라운드 부팅 — backend(:8000) / management(:50051,ROS2) / pyqt / web(:3001)
#   4) 포트 헬스체크 후 e2e_hw_full.py 워처 실행 (운영자에게 단계별 안내)
#   5) Ctrl+C 또는 종료 시 4 서비스 자동 정리
#
# 사용:
#   bash scripts/run-e2e-hw-full.sh
#   bash scripts/run-e2e-hw-full.sh --skip-jetson           # Jetson SSH 점검 생략
#   bash scripts/run-e2e-hw-full.sh --product=R-D450        # 발주 product_id 지정 (기본 R-D450)
#   bash scripts/run-e2e-hw-full.sh --keep                  # 성공 후 테스트 row 보존
#   E2E_TIMEOUT_SEC=600 bash scripts/run-e2e-hw-full.sh     # 단계별 타임아웃 (기본 300s)

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
log()  { echo -e "${Y}[hw-e2e]${N} $*"; }
ok()   { echo -e "${G}  ✓${N} $*"; }
warn() { echo -e "${Y}  !${N} $*"; }
fail() { echo -e "${R}  ✗${N} $*"; exit 1; }

# ─── 옵션 파싱 ─────────────────────────────────────────────
KEEP_DATA=""
SKIP_JETSON=0
ORDER_PRODUCT="R-D450"
for arg in "$@"; do
  case "$arg" in
    --keep)              KEEP_DATA="--keep-data" ;;
    --skip-jetson)       SKIP_JETSON=1 ;;
    --product=*)         ORDER_PRODUCT="${arg#--product=}" ;;
    -h|--help)
        sed -n '2,30p' "$0"; exit 0 ;;
    *) fail "알 수 없는 인자: $arg (--keep | --skip-jetson | --product=<id>)" ;;
  esac
done

echo "================================================================"
echo "  HW Full E2E — RA + TAT + CONV1"
echo "  product=$ORDER_PRODUCT  skip-jetson=$SKIP_JETSON  keep=${KEEP_DATA:-no}"
echo "================================================================"

# ─── 1) .env.local 로드 ────────────────────────────────────
log "[1/6] .env.local 로드"
ENV_FILE="$ROOT/server/main_service/.env.local"
[ -f "$ENV_FILE" ] || fail "$ENV_FILE 없음 — scripts/setup.sh 먼저 실행"
set -a; . "$ENV_FILE"; set +a
ok ".env.local 로드 완료"

# ─── 2) 사전 도구 점검 ─────────────────────────────────────
log "[2/6] 사전 도구 점검"
[ -n "${DATABASE_URL:-}" ] || fail "DATABASE_URL 미설정"
command -v psql >/dev/null || fail "psql 필요 — sudo apt install postgresql-client"
command -v nc   >/dev/null || fail "nc 필요 — sudo apt install netcat-openbsd"
PG_PSQL_URL="$(echo "$DATABASE_URL" | sed -E 's|postgresql\+psycopg(2?)://|postgresql://|')"
psql "$PG_PSQL_URL" -c 'SELECT 1' >/dev/null 2>&1 || fail "RDS 접속 실패 — DATABASE_URL 확인"
ok "RDS 접속 OK"

PY="$ROOT/server/main_service/.venv/bin/python"
[ -x "$PY" ] || fail "$PY 없음 — scripts/setup.sh"
ok "main_service .venv OK"

ROS2_SETUP="${MGMT_ROS2_SETUP:-/opt/ros/jazzy/setup.bash}"
[ -f "$ROS2_SETUP" ] || fail "ROS2 setup 없음: $ROS2_SETUP — ROS2 jazzy 설치 또는 MGMT_ROS2_SETUP export"
ok "ROS2 setup: $ROS2_SETUP"

[ -d "$ROOT/ui/web/node_modules" ] || fail "ui/web/node_modules 없음 — scripts/setup.sh"
[ -x "$ROOT/ui/pyqt/factory_operator/.venv/bin/python" ] || fail "PyQt .venv 없음 — scripts/setup.sh"
ok "PyQt + Web 의존성 OK"

# ─── 3) Jetson 점검 (옵션) ─────────────────────────────────
log "[3/6] Jetson 측 서비스 점검"
if [ "$SKIP_JETSON" -eq 1 ]; then
  warn "--skip-jetson — Jetson 점검 건너뜀"
else
  JETSON_HOST="${MGMT_JETSON_HOST:-}"
  JETSON_USER="${MGMT_JETSON_USER:-jetson}"
  if [ -z "$JETSON_HOST" ]; then
    warn "MGMT_JETSON_HOST 미설정 — Jetson 점검 건너뜀 (.env.local 에 추가하면 자동 점검)"
  else
    JETSON_SSH="${JETSON_USER}@${JETSON_HOST}"
    log "    SSH 시도: $JETSON_SSH"
    if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$JETSON_SSH" 'true' 2>/dev/null; then
      fail "Jetson SSH 실패 — ssh-copy-id ${JETSON_SSH} / Tailscale 점검 또는 --skip-jetson"
    fi
    ok "    SSH 도달"

    svc_state=$(ssh "$JETSON_SSH" 'systemctl is-active casting-image-publisher.service' 2>/dev/null || echo "unknown")
    if [ "$svc_state" != "active" ]; then
      warn "    casting-image-publisher.service: $svc_state — 재시작"
      ssh "$JETSON_SSH" 'sudo systemctl restart casting-image-publisher.service' \
        || fail "systemctl restart 실패 — sudoers NOPASSWD 또는 수동 sudo systemctl restart 필요"
      sleep 3
      svc_state=$(ssh "$JETSON_SSH" 'systemctl is-active casting-image-publisher.service' 2>/dev/null || echo "unknown")
    fi
    [ "$svc_state" = "active" ] || fail "casting-image-publisher.service still $svc_state"
    ok "    casting-image-publisher.service active"
  fi
fi

# ─── 4) 기존 인스턴스 정리 + 4 서비스 부팅 ─────────────────
log "[4/6] 기존 서비스 정리 + 4 서비스 백그라운드 부팅"
"$ROOT/scripts/stop-all.sh" >/dev/null 2>&1 || true
sleep 1

mkdir -p "$ROOT/logs"
declare -a SVC_PIDS=()
declare -a SVC_LABELS=()

start_svc() {
  local label="$1" command="$2" logfile="$3"
  : > "$logfile"
  nohup bash -c "$command" > "$logfile" 2>&1 &
  local pid=$!
  SVC_PIDS+=("$pid"); SVC_LABELS+=("$label")
  echo "    → $label  PID=$pid  log=$logfile"
}

start_svc "backend"    "$ROOT/scripts/run-backend.sh"        "$ROOT/logs/backend.log"
start_svc "management" "$ROOT/scripts/run-management.sh ros" "$ROOT/logs/management.log"
start_svc "pyqt"       "$ROOT/scripts/run-pyqt.sh"           "$ROOT/logs/pyqt.log"
start_svc "web"        "$ROOT/scripts/run-web.sh"            "$ROOT/logs/web.log"

cleanup() {
  local rc=$?
  echo
  log "[cleanup] 서비스 종료 (rc=$rc)"
  for i in "${!SVC_PIDS[@]}"; do
    local pid="${SVC_PIDS[$i]}" label="${SVC_LABELS[$i]}"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      pkill -P "$pid" 2>/dev/null || true
      echo "    $label (pid=$pid) terminated"
    fi
  done
  exit $rc
}
trap cleanup EXIT INT TERM

# ─── 5) 포트 헬스체크 ──────────────────────────────────────
log "[5/6] 포트 헬스체크"
wait_port() {
  local host="$1" port="$2" label="$3" timeout="${4:-45}"
  for _ in $(seq 1 "$timeout"); do
    if nc -z "$host" "$port" 2>/dev/null; then
      ok "    $label :$port"
      return 0
    fi
    sleep 1
  done
  fail "$label :$port 미응답 (timeout=${timeout}s) — tail -n 50 logs/${label}.log"
}

wait_port 127.0.0.1 8000  "backend"    60
wait_port 127.0.0.1 50051 "management" 60
wait_port 127.0.0.1 3001  "web"        90

# ─── 6) e2e 워처 실행 ──────────────────────────────────────
log "[6/6] HW e2e 워처 실행"
echo
echo "    ┌─────────────────────────────────────────────────────────────┐"
echo "    │ 운영자 안내가 화면에 출력됩니다.                              │"
echo "    │ PyQt 에서 단계별 advance 버튼을, 라인에서 핸드오프 버튼/RFID/ │"
echo "    │ 검사 위치 정지를 차례대로 수행하세요.                         │"
echo "    │ Ctrl+C 로 중단 시 4 서비스 모두 자동 종료됩니다.              │"
echo "    └─────────────────────────────────────────────────────────────┘"
echo

E2E_PY="$ROOT/server/main_service/scripts/e2e/e2e_hw_full.py"
[ -f "$E2E_PY" ] || fail "$E2E_PY 없음 — git pull 누락?"

TIMEOUT_SEC="${E2E_TIMEOUT_SEC:-300}"
PYTHONPATH="$ROOT/server/main_service/src/management_service:$ROOT/server/main_service/src:$ROOT/server" \
  "$PY" "$E2E_PY" \
    --product "$ORDER_PRODUCT" \
    --timeout "$TIMEOUT_SEC" \
    $KEEP_DATA
RC=$?

echo
if [ "$RC" -eq 0 ]; then
  ok "HW e2e 통과"
else
  warn "HW e2e 실패 (exit=$RC) — 로그 확인:"
  echo "    tail -n 80 logs/backend.log"
  echo "    tail -n 80 logs/management.log"
  exit "$RC"
fi
