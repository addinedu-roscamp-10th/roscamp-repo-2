#!/usr/bin/env bash
# management bridge가 먼저 떠야 각 로봇의 client bridge가 접속할 수 있음

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${ROOT}/bridge_management.json5"

if [[ ! -f "$CONFIG" ]]; then
    echo "✗ config 없음: $CONFIG" >&2
    exit 1
fi

if ! command -v zenoh-bridge-ros2dds >/dev/null 2>&1; then
    echo "✗ zenoh-bridge-ros2dds 설치 필요." >&2
    exit 1
fi

if ! ls /opt/ros/*/lib/librmw_cyclonedds_cpp.so >/dev/null 2>&1; then
    echo "✗ rmw_cyclonedds_cpp 설치 필요." >&2
    exit 1
fi

if ! sed 's|//.*||' "$CONFIG" | grep -qE 'domain\s*:\s*[0-9]+'; then
    echo "✗ ${CONFIG}에 domain 설정 필요." >&2
    exit 1
fi

export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"

echo "[management-bridge] RMW=${RMW_IMPLEMENTATION}"
echo "[management-bridge] config=${CONFIG}"
echo "[management-bridge] listening on tcp/0.0.0.0:7447 ..."
echo

exec zenoh-bridge-ros2dds -c "$CONFIG"
