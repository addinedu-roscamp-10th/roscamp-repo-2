#!/usr/bin/env bash
# Mock AI service :8100 실행 — 검사 이미지 수신 + 양/불 mock 판정.
#
# main_service AI 어댑터가 본 mock 으로 검사 이미지를 forward 한다. e2e 컨베이어
# 통합 테스트 동안 실 AI 추론 모델 (다른 담당) 가 없을 때 round-robin OK/NG 응답을
# 돌려주어 backend chain (DB INSERT + INSP_COMPLETED publish) 을 검증할 수 있다.
#
# 환경변수:
#   AI_SERVICE_HOST     - bind host (기본 127.0.0.1)
#   AI_SERVICE_PORT     - bind port (기본 8100)
#   AI_INSP_IMAGE_ROOT  - 저장 루트 (기본 ./Inspection_Image)
#   AI_MOCK_MODE        - always_pass | always_fail | round_robin | random (기본 round_robin)
#   AI_MOCK_PASS_RATIO  - random 모드 OK 비율 (기본 0.7)

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/server/ai_service"

VENV="${ROOT}/server/ai_service/.venv"
if [ ! -d "$VENV" ]; then
    echo "[run-ai-mock] .venv 미존재 — 생성 + 의존성 설치"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q -U pip
    "$VENV/bin/pip" install -q -r requirements.txt
fi

PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "✗ $PY 없음 — venv 재생성 필요"; exit 1; }

export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
export AI_SERVICE_HOST="${AI_SERVICE_HOST:-127.0.0.1}"
export AI_SERVICE_PORT="${AI_SERVICE_PORT:-8100}"
export AI_INSP_IMAGE_ROOT="${AI_INSP_IMAGE_ROOT:-$ROOT/server/ai_service/Inspection_Image}"
export AI_MOCK_MODE="${AI_MOCK_MODE:-round_robin}"

echo "[run-ai-mock] http://$AI_SERVICE_HOST:$AI_SERVICE_PORT  mode=$AI_MOCK_MODE  root=$AI_INSP_IMAGE_ROOT"
exec "$PY" -m ai_service
