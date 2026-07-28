#!/usr/bin/env bash
# HW 풀 e2e 단일 배치 — Ubuntu 백엔드 개발자가 git pull dev 직후 한 번에 실행.
#
# 검증 대상:
#   RA 자율 (MM/POUR/DM) → TAT 운반 (ToPP) → 운영자 ① 하차완료 →
#   ② RFID → ③ 후처리 완료 → CONV1 → TOF2 → 카메라 → AI 추론 →
#   양품(GP) / 불량(DP, RA 재진입) 분기 → 최종 일관성 검증
#
# 사전 조건 (스크립트가 모두 진단해 안내):
#   - Ubuntu 24.04 + scripts/setup.sh 1회 실행 완료 (.venv, node_modules, .env.local)
#   - ROS2 jazzy 설치 (/opt/ros/jazzy/setup.bash 또는 MGMT_ROS2_SETUP)
#   - Tailscale + Jetson SSH 키 (MGMT_JETSON_HOST/USER); --skip-jetson 으로 우회 가능
#   - 실 RA 암(ROS2), TAT AMR(ROS2), CONV1(ESP32 v1.7.0+) 라인 연결
#   - AWS RDS Casting 스키마 접속 (DATABASE_URL)
#
# 단계:
#   [A] 도구 점검 (python/node/psql/nc/jq/ssh/ros2)
#   [B] 프로젝트 셋업 점검 (.venv × 2, node_modules, .env.local, proto 스텁)
#   [C] DB 연결 + 핵심 테이블 13 종 존재 확인
#   [D] Jetson SSH + casting-image-publisher.service active (옵션)
#   [E] ROS2 RA/TAT 토픽 prob (옵션)
#   [F] 4 서비스 부팅: backend(:8000) / management(:50051,ROS2) / pyqt / web(:3001)
#   [G] 헬스체크: HTTP 200 (backend openapi, web /), 포트 open (50051)
#   [H] e2e_hw_full.py 워처 실행 (Phase 1~10)
#   [I] Ctrl+C 또는 종료 시 4 서비스 자동 정리
#
# 옵션:
#   --skip-jetson           Jetson SSH 점검 생략
#   --skip-ros2-probe       ros2 topic list 점검 생략
#   --skip-services         이미 띄워둔 서비스 사용 (부팅 안 함)
#   --product=<id>          발주 product_id (기본 R-D450)
#   --keep                  성공 후 테스트 row 보존
#   --timeout=<sec>         단계별 폴링 타임아웃 (기본 300)
#   -h, --help              도움말

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ─── 색상 + 헬퍼 ──────────────────────────────────────
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; CY='\033[0;36m'; B='\033[1;34m'; N='\033[0m'
hdr()  { echo; echo -e "${B}══ $* ══${N}"; }
info() { echo -e "${Y}[hw-e2e]${N} $*"; }
ok()   { echo -e "  ${G}✓${N} $*"; }
warn() { echo -e "  ${Y}!${N} $*"; }
err()  { echo -e "  ${R}✗${N} $*" >&2; }
fail() { err "$*"; exit 1; }

# ─── 옵션 파싱 ─────────────────────────────────────────
KEEP_DATA=""
SKIP_JETSON=0
SKIP_ROS2_PROBE=0
SKIP_SERVICES=0
ORDER_PRODUCT="R-D450"
TIMEOUT_SEC="${E2E_TIMEOUT_SEC:-300}"

for arg in "$@"; do
  case "$arg" in
    --keep)                KEEP_DATA="--keep-data" ;;
    --skip-jetson)         SKIP_JETSON=1 ;;
    --skip-ros2-probe)     SKIP_ROS2_PROBE=1 ;;
    --skip-services)       SKIP_SERVICES=1 ;;
    --product=*)           ORDER_PRODUCT="${arg#--product=}" ;;
    --timeout=*)           TIMEOUT_SEC="${arg#--timeout=}" ;;
    -h|--help)
        sed -n '2,40p' "$0"; exit 0 ;;
    *) fail "알 수 없는 인자: $arg (--help)" ;;
  esac
done

echo "================================================================"
echo "  HW Full E2E — RA → TAT → CONV1 → (불량 시) RA 재진입"
echo "  product=$ORDER_PRODUCT  timeout=${TIMEOUT_SEC}s  keep=${KEEP_DATA:-no}"
echo "  skip: jetson=$SKIP_JETSON ros2=$SKIP_ROS2_PROBE services=$SKIP_SERVICES"
echo "================================================================"

# ─── [A] 도구 점검 ─────────────────────────────────────
hdr "[A] 도구 점검"

need_tool() {
  local cmd="$1" install_hint="$2"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd"
  else
    err "$cmd 미설치"
    echo "         → 설치: $install_hint"
    return 1
  fi
}

need_tool python3.12  "sudo apt install python3.12 python3.12-venv" || \
  need_tool python3.11 "sudo apt install python3.11 python3.11-venv" || \
  fail "Python 3.11 또는 3.12 필요"
need_tool node    "Node.js 20+ 설치 (https://nodejs.org)"
need_tool npm     "Node.js 와 함께 설치"
need_tool psql    "sudo apt install postgresql-client"
need_tool nc      "sudo apt install netcat-openbsd"
need_tool jq      "sudo apt install jq"
need_tool ssh     "sudo apt install openssh-client"
need_tool curl    "sudo apt install curl"

# ROS2 setup 파일 확인 (ros2 명령 자체는 source 후에만 활성)
ROS2_SETUP="${MGMT_ROS2_SETUP:-/opt/ros/jazzy/setup.bash}"
if [ -f "$ROS2_SETUP" ]; then
  ok "ROS2 setup: $ROS2_SETUP"
else
  err "ROS2 setup 없음: $ROS2_SETUP"
  echo "         → 설치: sudo apt install ros-jazzy-desktop"
  echo "         → 또는: export MGMT_ROS2_SETUP=<your-path>"
  fail "ROS2 미설치"
fi

# ─── [B] 프로젝트 셋업 점검 ───────────────────────────
hdr "[B] 프로젝트 셋업 점검"

ENV_FILE="$ROOT/server/main_service/.env.local"
if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$ROOT/server/main_service/.env.example" ]; then
    err ".env.local 없음 — .env.example 을 복사하고 비밀번호 채워 넣으세요"
    echo "         cp server/main_service/.env.example $ENV_FILE"
  fi
  fail "$ENV_FILE 없음 — scripts/setup.sh 실행 필요"
fi
ok "$ENV_FILE 존재"

# .env.local 로드
set -a; . "$ENV_FILE"; set +a
[ -n "${DATABASE_URL:-}" ] || fail "DATABASE_URL 미설정"
ok "DATABASE_URL 로드"

BACKEND_VENV="$ROOT/server/main_service/.venv"
PYQT_VENV="$ROOT/ui/pyqt/factory_operator/.venv"

[ -x "$BACKEND_VENV/bin/python" ] || fail "$BACKEND_VENV 없음 — scripts/setup.sh"
"$BACKEND_VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
  || fail "backend .venv 가 Python 3.11 미만 — scripts/setup.sh 재실행"
ok "backend .venv (Python $($BACKEND_VENV/bin/python -c 'import sys; print(sys.version.split()[0])'))"

"$BACKEND_VENV/bin/python" -c 'import httpx, sqlalchemy, fastapi, uvicorn' 2>/dev/null \
  || fail "backend .venv 핵심 패키지(httpx/sqlalchemy/fastapi/uvicorn) 누락 — scripts/setup.sh 재실행"
ok "backend 핵심 패키지 OK"

[ -x "$PYQT_VENV/bin/python" ] || fail "$PYQT_VENV 없음 — scripts/setup.sh"
ok "pyqt .venv"

[ -d "$ROOT/ui/web/node_modules" ] || fail "ui/web/node_modules 없음 — scripts/setup.sh"
ok "ui/web/node_modules"

PROTO1="$ROOT/server/main_service/src/management_service/management_pb2.py"
PROTO2="$ROOT/server/main_service/src/management_service/management_pb2_grpc.py"
if [ ! -f "$PROTO1" ] || [ ! -f "$PROTO2" ]; then
  warn "proto 스텁 누락 — scripts/gen_proto.sh 자동 실행"
  "$ROOT/scripts/gen_proto.sh" || fail "proto 빌드 실패 — scripts/gen_proto.sh 확인"
fi
ok "proto 스텁 (management_pb2.py + management_pb2_grpc.py)"

# ─── [C] DB 연결 + 핵심 테이블 ─────────────────────────
hdr "[C] DB 연결 + 핵심 테이블"

PG_URL="$(echo "$DATABASE_URL" | sed -E 's|postgresql\+psycopg(2?)://|postgresql://|')"
psql "$PG_URL" -c 'SELECT 1' >/dev/null 2>&1 || fail "RDS 접속 실패 — DATABASE_URL / VPN 확인"
ok "RDS 접속 OK"

PG_SCHEMA="${PG_SCHEMA:-${SMARTCAST_SCHEMA:-smartcast}}"
ok "스키마: $PG_SCHEMA"

CORE_TABLES=(
  ord ord_pattern ord_pp_map ord_stat
  item equip_task_txn trans_task_txn
  pp_task_txn
  log_action_operator_rfid_scan
  log_action_operator_handoff_acks
  insp_task_txn ai_inference_txn insp_stat
)
missing=()
for t in "${CORE_TABLES[@]}"; do
  if ! psql "$PG_URL" -tAc "SELECT 1 FROM ${PG_SCHEMA}.${t} LIMIT 1" >/dev/null 2>&1; then
    missing+=("$t")
  fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  for t in "${missing[@]}"; do err "$PG_SCHEMA.$t 존재하지 않음"; done
  fail "핵심 테이블 누락 — DB 마이그레이션 필요 (server/smart_cast_db/migrations)"
fi
ok "핵심 테이블 13 종 모두 존재"

# ─── [D] Jetson 점검 (옵션) ───────────────────────────
hdr "[D] Jetson 점검"
if [ "$SKIP_JETSON" -eq 1 ]; then
  warn "--skip-jetson — Jetson 점검 건너뜀"
else
  JETSON_HOST="${MGMT_JETSON_HOST:-}"
  JETSON_USER="${MGMT_JETSON_USER:-jetson}"
  if [ -z "$JETSON_HOST" ]; then
    warn "MGMT_JETSON_HOST 미설정 — .env.local 에 추가하면 자동 점검 (--skip-jetson 와 동일)"
  else
    JETSON_SSH="${JETSON_USER}@${JETSON_HOST}"
    info "    SSH 시도: $JETSON_SSH"
    if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$JETSON_SSH" 'true' 2>/dev/null; then
      err "Jetson SSH 실패"
      echo "         → ssh-copy-id $JETSON_SSH 로 키 등록"
      echo "         → 또는 --skip-jetson 로 우회"
      fail "Jetson 도달 불가"
    fi
    ok "    SSH 도달 ($JETSON_SSH)"

    svc_state=$(ssh "$JETSON_SSH" 'systemctl is-active casting-image-publisher.service' 2>/dev/null || echo "unknown")
    if [ "$svc_state" != "active" ]; then
      warn "    casting-image-publisher.service: $svc_state — 재시작 시도"
      ssh "$JETSON_SSH" 'sudo systemctl restart casting-image-publisher.service' \
        || fail "systemctl restart 실패 — Jetson 에 sudoers NOPASSWD 또는 수동 실행"
      sleep 3
      svc_state=$(ssh "$JETSON_SSH" 'systemctl is-active casting-image-publisher.service' 2>/dev/null || echo "unknown")
    fi
    [ "$svc_state" = "active" ] || fail "casting-image-publisher.service still $svc_state"
    ok "    casting-image-publisher.service active"

    if ssh "$JETSON_SSH" 'test -e /dev/ttyUSB0' 2>/dev/null; then
      ok "    /dev/ttyUSB0 (ESP32 컨베이어 직렬) 존재"
    else
      warn "    /dev/ttyUSB0 없음 — ESP32 USB 연결 확인"
    fi
  fi
fi

# ─── [E] ROS2 RA/TAT 토픽 prob (옵션) ─────────────────
hdr "[E] ROS2 토픽 prob"
if [ "$SKIP_ROS2_PROBE" -eq 1 ]; then
  warn "--skip-ros2-probe — ROS2 토픽 점검 건너뜀"
else
  # source ROS2 in subshell (오염 방지)
  if ( . "$ROS2_SETUP" && timeout 5 ros2 topic list >/dev/null 2>&1 ); then
    topics=$( . "$ROS2_SETUP" && timeout 5 ros2 topic list 2>/dev/null )
    if echo "$topics" | grep -qiE "arm|smartcast_arm|ra"; then
      ok "    RA arm 토픽 발견"
    else
      warn "    RA arm 토픽 없음 — 컨트롤러 미동작일 수 있음"
    fi
    if echo "$topics" | grep -qiE "amr|tat|smartcast_amr"; then
      ok "    TAT AMR 토픽 발견"
    else
      warn "    TAT AMR 토픽 없음 — AMR 컨트롤러 미동작일 수 있음"
    fi
  else
    warn "    ros2 topic list 실패 — ROS2 daemon 또는 도메인 미설정"
  fi
fi

# ─── [F] 4 서비스 부팅 ────────────────────────────────
hdr "[F] 4 서비스 부팅"
declare -a SVC_PIDS=()
declare -a SVC_LABELS=()
mkdir -p "$ROOT/logs"

if [ "$SKIP_SERVICES" -eq 1 ]; then
  warn "--skip-services — 부팅 생략, 이미 띄운 서비스 사용"
else
  # 기존 인스턴스 정리
  "$ROOT/scripts/stop-all.sh" >/dev/null 2>&1 || true
  sleep 1

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
fi

cleanup() {
  local rc=$?
  echo
  info "[cleanup] 서비스 종료 (rc=$rc)"
  for i in "${!SVC_PIDS[@]}"; do
    local pid="${SVC_PIDS[$i]}" label="${SVC_LABELS[$i]}"
    if kill -0 "$pid" 2>/dev/null; then
      pkill -P "$pid" 2>/dev/null || true
      kill "$pid" 2>/dev/null || true
      echo "    $label (pid=$pid) terminated"
    fi
  done
  exit $rc
}
trap cleanup EXIT INT TERM

# ─── [G] 서비스 헬스체크 ───────────────────────────────
hdr "[G] 서비스 헬스체크"

wait_port() {
  local host="$1" port="$2" label="$3" timeout="${4:-60}"
  for _ in $(seq 1 "$timeout"); do
    if nc -z "$host" "$port" 2>/dev/null; then
      ok "    $label :$port port open"
      return 0
    fi
    sleep 1
  done
  err "$label :$port 미응답 (timeout=${timeout}s)"
  echo "         → tail -n 80 logs/${label}.log"
  return 1
}

wait_http() {
  local url="$1" label="$2" timeout="${3:-60}"
  for _ in $(seq 1 "$timeout"); do
    if curl -fsS --max-time 3 "$url" -o /dev/null 2>/dev/null; then
      ok "    $label $url HTTP 200"
      return 0
    fi
    sleep 1
  done
  err "$label $url 미응답 (timeout=${timeout}s)"
  return 1
}

wait_port 127.0.0.1 8000  "backend"    60 || fail "backend 미부팅"
wait_http "http://127.0.0.1:8000/openapi.json" "backend openapi" 30 || fail "backend FastAPI 비정상"
wait_port 127.0.0.1 50051 "management" 60 || fail "management gRPC 미부팅"
wait_port 127.0.0.1 3001  "web"        90 || warn "web 미응답 (계속 진행 — Phase 0 에서 다시 확인)"

# PyQt 프로세스 alive
if [ "$SKIP_SERVICES" -ne 1 ]; then
  pyqt_pid="${SVC_PIDS[2]:-}"
  if [ -n "$pyqt_pid" ] && kill -0 "$pyqt_pid" 2>/dev/null; then
    ok "    pyqt 프로세스 PID=$pyqt_pid alive"
  else
    warn "    pyqt 프로세스 사망 — DISPLAY 환경 또는 PyQt 의존성 점검"
    echo "         → tail -n 80 logs/pyqt.log"
  fi
fi

# ─── [H] e2e 워처 실행 ─────────────────────────────────
hdr "[H] e2e 워처 실행"
echo
echo "    ┌────────────────────────────────────────────────────────────┐"
echo "    │ Phase 0~10 이 차례로 진행됩니다.                              │"
echo "    │ 운영자 액션이 필요한 단계 (Phase 5/6/7) 에서는 화면 안내에       │"
echo "    │ 따라 PyQt 버튼 또는 라인 하드웨어를 조작하세요.                  │"
echo "    │ Ctrl+C 로 중단하면 4 서비스가 자동 종료됩니다.                  │"
echo "    └────────────────────────────────────────────────────────────┘"
echo

E2E_PY="$ROOT/server/main_service/scripts/e2e/e2e_hw_full.py"
[ -f "$E2E_PY" ] || fail "$E2E_PY 없음 — git pull 누락?"

PYTHONPATH="$ROOT/server/main_service/src/management_service:$ROOT/server/main_service/src:$ROOT/server" \
  "$BACKEND_VENV/bin/python" "$E2E_PY" \
    --product "$ORDER_PRODUCT" \
    --timeout "$TIMEOUT_SEC" \
    $KEEP_DATA
RC=$?

echo
if [ "$RC" -eq 0 ]; then
  ok "HW Full E2E 통과"
else
  err "HW Full E2E 실패 (exit=$RC)"
  echo "    실패 단계별 추적:"
  echo "      11: 발주 생성    | 12: APPR        | 13: 생산 시작"
  echo "      14: RA 자율      | 15: 핸드오프    | 16: RFID"
  echo "      17: PP 완료      | 18: 검사/AI     | 19: 결과 분기"
  echo "      20: 일관성 검증"
  echo
  echo "    로그 위치:"
  echo "      tail -n 80 logs/backend.log    # FastAPI :8000"
  echo "      tail -n 80 logs/management.log # gRPC :50051 (ROS2)"
  echo "      tail -n 80 logs/pyqt.log       # PyQt 운영자 UI"
  echo "      tail -n 80 logs/web.log        # Next.js :3001"
  exit "$RC"
fi
