#!/usr/bin/env bash
# 4 서비스 동시 실행 — GUI 터미널 4개 또는 백그라운드 fallback.
#
# GUI 터미널이 있으면 backend / management / pyqt / web 을 각각 별도 창으로 실행.
# GUI 터미널을 열 수 없으면 nohup 으로 백그라운드 실행 + logs/ 디렉터리에 출력.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
MANAGEMENT_ARGS=()
DO_RESET=0

for arg in "$@"; do
  case "$arg" in
    "")
      ;;
    ros)
      MANAGEMENT_ARGS=("ros")
      ;;
    reset)
      DO_RESET=1
      ;;
    *)
      echo "✗ 잘못된 인자: $arg (허용값: ros, reset)"
      exit 1
      ;;
  esac
done

if [ "$DO_RESET" = "1" ]; then
  RESET_PY="$ROOT/server/main_service/.venv/bin/python"
  RESET_SCRIPT="$ROOT/server/main_service/scripts/reset_mfg_orders.py"
  if [ ! -x "$RESET_PY" ]; then
    echo "✗ reset: $RESET_PY 없음. server/main_service 의 venv 를 먼저 준비하세요."
    exit 1
  fi
  if [ ! -f "$RESET_SCRIPT" ]; then
    echo "✗ reset: $RESET_SCRIPT 없음."
    exit 1
  fi
  echo "→ MFG 주문 APPR 복귀 정리 시작 (2초 후 진행, 취소: Ctrl+C)"
  "$RESET_PY" "$RESET_SCRIPT" --delay 2
fi

TERMS_PIDS_FILE="/tmp/smartcast_terms.pids"
# 이전 실행 PID 파일 초기화
rm -f "$TERMS_PIDS_FILE"

open_terminal() {
  local title="$1"
  local command="$2"
  local hold_command
  # $$ (bash PID) 를 파일에 기록 → stop-all.sh 에서 창 닫기에 사용
  printf -v hold_command \
    'echo $$ >> %q; cd %q; %s; status=$?; echo; echo "[%s] exited (status=${status})"; read -r -p "Press Enter to close..."' \
    "$TERMS_PIDS_FILE" "$ROOT" "$command" "$title"

  # gnome-terminal 은 클라이언트-서버 구조라 & 없이도 즉시 반환.
  # 에러는 stderr 에 출력되므로 >/dev/null 2>&1 제거 → 실패 시 exit code 감지 가능.
  if command -v gnome-terminal >/dev/null 2>&1; then
    gnome-terminal --title="$title" -- bash -lc "$hold_command" 2>/dev/null
  elif command -v konsole >/dev/null 2>&1; then
    konsole --new-tab --title "$title" -e bash -lc "$hold_command" 2>/dev/null &
  elif command -v xfce4-terminal >/dev/null 2>&1; then
    xfce4-terminal --title="$title" --command="bash -lc $(printf '%q' "$hold_command")" 2>/dev/null &
  elif command -v mate-terminal >/dev/null 2>&1; then
    mate-terminal --title="$title" -- bash -lc "$hold_command" 2>/dev/null &
  elif command -v lxterminal >/dev/null 2>&1; then
    lxterminal --title="$title" -e bash -lc "$hold_command" 2>/dev/null &
  elif command -v xterm >/dev/null 2>&1; then
    xterm -T "$title" -e bash -lc "$hold_command" 2>/dev/null &
  else
    return 1
  fi
}

management_command="$ROOT/scripts/run-management.sh"
if [ "${#MANAGEMENT_ARGS[@]}" -gt 0 ]; then
  management_command+=" ${MANAGEMENT_ARGS[*]}"
fi

if open_terminal "smartcast-backend" "$ROOT/scripts/run-backend.sh"; then
  open_terminal "smartcast-management" "$management_command"
  open_terminal "smartcast-pyqt" "$ROOT/scripts/run-pyqt.sh"
  open_terminal "smartcast-web" "$ROOT/scripts/run-web.sh"
  echo "→ GUI 터미널 창 4개 시작 (backend / management / pyqt / web, management ROS2=$([ "${#MANAGEMENT_ARGS[@]}" -gt 0 ] && echo enabled || echo disabled))"
  echo "  중단:    ./scripts/stop-all.sh"
else
  mkdir -p "$ROOT/logs"
  echo "→ GUI 터미널 없음 — 백그라운드 + logs/*.log 로 출력"
  nohup "$ROOT/scripts/run-backend.sh" > "$ROOT/logs/backend.log" 2>&1 &
  echo "  backend  PID=$!"
  nohup "$ROOT/scripts/run-management.sh" > "$ROOT/logs/management.log" 2>&1 &
  echo "  management PID=$!"
  nohup "$ROOT/scripts/run-pyqt.sh"    > "$ROOT/logs/pyqt.log"    2>&1 &
  echo "  pyqt     PID=$!"
  nohup "$ROOT/scripts/run-web.sh"     > "$ROOT/logs/web.log"     2>&1 &
  echo "  web      PID=$!"
  echo "  로그:    tail -f logs/{backend,management,pyqt,web}.log"
  echo "  중단:    ./scripts/stop-all.sh"
fi
