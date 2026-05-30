#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${ROOT}/bridge_MAT.json5"

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
    echo "✗ ${CONFIG}에 domain id 설정 필요." >&2
    exit 1
fi

export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"

echo "[MAT-bridge] RMW=${RMW_IMPLEMENTATION}"
echo "[MAT-bridge] config=${CONFIG}"
echo

exec zenoh-bridge-ros2dds -c "$CONFIG"
