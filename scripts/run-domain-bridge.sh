#!/bin/bash
# Usage: ./run-domain-bridge.sh [TAT 번호...]
#   인자 없으면 1 2 3 4 5 전체 실행
#   예) ./run-domain-bridge.sh 1 3 4

set -e
BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/domain_bridge"

trap 'kill 0' EXIT INT TERM

if [[ $# -eq 0 ]]; then
  set -- 1 2 3 4 5
fi
for i in "$@"; do
  yaml="${BRIDGE_DIR}/TAT${i}_to_control.yaml"
  if [[ ! -f "$yaml" ]]; then
    echo "skip: $yaml not found" >&2
    continue
  fi
  echo "starting TAT${i} bridge ($yaml)"
  ros2 run domain_bridge domain_bridge "$yaml" \
    --ros-args -r __node:=tat${i}_domain_bridge &
done

wait
