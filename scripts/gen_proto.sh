#!/usr/bin/env bash

set -euo pipefail

TASK_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_PROTO_DIR="${TASK_REPO_ROOT}/server/main_service/src/management_service/rpc/proto"
TASK_SERVER_OUT="${TASK_REPO_ROOT}/server/main_service/src/management_service"
TASK_PYQT_OUT="${TASK_REPO_ROOT}/ui/pyqt/factory_operator/src/factory_operator/app/generated"
TASK_JETSON_OUT="${TASK_REPO_ROOT}/device/camera/src/camera/jetson_publisher/generated"

# Python 인터프리터 탐색 (PyQt venv -> Server venv -> 시스템 python3)
TASK_PROTO_PYTHON=""
if [[ -x "${TASK_REPO_ROOT}/ui/pyqt/factory_operator/.venv/bin/python" ]]; then
  TASK_PROTO_PYTHON="${TASK_REPO_ROOT}/ui/pyqt/factory_operator/.venv/bin/python"
elif [[ -x "${TASK_REPO_ROOT}/server/main_service/.venv/bin/python" ]]; then
  TASK_PROTO_PYTHON="${TASK_REPO_ROOT}/server/main_service/.venv/bin/python"
else
  TASK_PROTO_PYTHON="python3"
fi

echo "[gen_proto] Generating gRPC code from ${TASK_PROTO_DIR}/management.proto using ${TASK_PROTO_PYTHON}..."

for task_output_dir in "${TASK_SERVER_OUT}" "${TASK_PYQT_OUT}" "${TASK_JETSON_OUT}"; do
  mkdir -p "${task_output_dir}"
  "${TASK_PROTO_PYTHON}" -m grpc_tools.protoc \
    -I "${TASK_PROTO_DIR}" \
    --python_out="${task_output_dir}" \
    --grpc_python_out="${task_output_dir}" \
    "${TASK_PROTO_DIR}/management.proto"
done

for task_grpc_file in \
  "${TASK_PYQT_OUT}/management_pb2_grpc.py" \
  "${TASK_JETSON_OUT}/management_pb2_grpc.py"; do
  if [[ -f "${task_grpc_file}" ]]; then
    sed -i \
      's/^import management_pb2 as management__pb2$/from . import management_pb2 as management__pb2/' \
      "${task_grpc_file}"
  fi
done

echo "[gen_proto] gRPC code generation complete."
