#!/usr/bin/env bash
# Management gRPC service :50051 실행.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/server/main_service"

[ -d .venv ] || { echo "✗ .venv 없음. ./scripts/setup.sh 먼저 실행."; exit 1; }
[ -f .env.local ] || { echo "✗ .env.local 없음. setup.sh 먼저 실행 후 비밀번호 입력."; exit 1; }

PY=".venv/bin/python"
[ -x "$PY" ] || { echo "✗ $PY 없음. ./scripts/setup.sh 다시 실행."; exit 1; }

"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "backend .venv 가 Python 3.11 미만입니다. Python 3.11 설치 후 ./scripts/setup.sh 를 다시 실행하세요.")'
export PYTHONPATH="../:src/management_service:src/interface_service:src${PYTHONPATH:+:$PYTHONPATH}"

# ROS2 jazzy setup
ROS2_SETUP="${MGMT_ROS2_SETUP:-/opt/ros/jazzy/setup.bash}"
CAST_PYTHON_SETUP="${MGMT_CAST_PYTHON_SETUP:-$ROOT/device/smartcast_arm/mat/cast_python/install/local_setup.bash}"

enable_ros2=false
case "${1:-}" in
  "")
    enable_ros2=false
    ;;
  ros)
    enable_ros2=true
    ;;
  *)
    echo "✗ 잘못된 인자: ${1:-} (허용값: ros)"
    exit 1
    ;;
esac

if [ "$enable_ros2" = true ]; then
  if [ ! -f "$ROS2_SETUP" ]; then
    echo "✗ ROS2 base setup not found: $ROS2_SETUP"
    exit 1
  fi
  if [ ! -f "$CAST_PYTHON_SETUP" ]; then
    echo "✗ cast_python setup not found: $CAST_PYTHON_SETUP"
    exit 1
  fi

  # ros2 setup에 정의되지 않은 변수 때문에 튕기지 않도록 설정(set -u 잠시 off)
  had_nounset=false
  case $- in
    *u*)
      had_nounset=true
      set +u
      ;;
  esac

  . "$ROS2_SETUP"
  echo "→ sourced ROS2 base"
  . "$CAST_PYTHON_SETUP"
  echo "→ sourced cast_python overlay"

  if [ "$had_nounset" = true ]; then
    set -u
  fi
fi

HOST="${MANAGEMENT_GRPC_HOST:-0.0.0.0}"
PORT="${MANAGEMENT_GRPC_PORT:-50051}"
echo "→ Management gRPC on $HOST:$PORT (ROS2=$([ "$enable_ros2" = true ] && echo enabled || echo disabled), Ctrl+C 종료)"
exec "$PY" src/management_service/server.py
