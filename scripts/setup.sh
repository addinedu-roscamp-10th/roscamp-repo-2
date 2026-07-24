#!/usr/bin/env bash
# Ubuntu 24.04 기준으로 모든 모듈의 가상환경 생성 + 의존성 설치 + .env.local 템플릿 복사.
# Idempotent — 여러 번 실행해도 안전.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

YELLOW='\033[1;33m'; GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${YELLOW}[setup]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
fail() { echo -e "${RED}  ✗${NC} $*"; exit 1; }

ensure_apt_package() {
  local pkg="$1"
  if ! dpkg -s "$pkg" >/dev/null 2>&1; then
    log "Ubuntu 패키지 설치: $pkg"
    sudo apt-get update
    sudo apt-get install -y "$pkg"
  fi
}

# 1. 사전 도구 점검
log "Ubuntu 24.04 사전 도구 점검"
ensure_apt_package build-essential
ensure_apt_package curl
ensure_apt_package libpq-dev
ensure_apt_package python3-pip
ensure_apt_package python3-venv
ensure_apt_package python3.12
ensure_apt_package python3.12-venv
command -v node >/dev/null || fail "node 필요 (Ubuntu 24.04에서는 Node.js 20+ 설치 후 다시 실행하세요)"
command -v npm  >/dev/null || fail "npm 필요 (Node.js 설치 시 함께 제공됩니다)"
command -v python3.12 >/dev/null || fail "python3.12 필요"
ok "Ubuntu 패키지와 python/node/npm 확인"

# AWS RDS CA bundle
PEM_PATH="$ROOT/global-bundle.pem"
if [ -f "$PEM_PATH" ]; then
  ok "AWS RDS CA bundle already exists at $PEM_PATH"
else
  log "AWS RDS CA bundle 다운로드"
  curl -fsSL "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem" -o "$PEM_PATH"
  ok "global-bundle.pem 저장 완료: $PEM_PATH"
fi

# 2. backend
log "[1/3] server/main_service venv + 의존성"
cd "$ROOT/server/main_service"
PY=$(command -v python3.12 || command -v python3.11 || command -v python3)
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  log "기존 server/main_service/.venv 가 Python 3.11 미만이라 재생성"
  rm -rf .venv
fi
[ -d .venv ] || $PY -m venv .venv
"./.venv/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "backend 는 Python 3.11 이상이 필요합니다.")'
"./.venv/bin/pip" install --upgrade pip --quiet
"./.venv/bin/pip" install -r requirements.txt --quiet
if [ ! -f .env.local ]; then
  sed "s|<REPO_ROOT>|$ROOT|g" .env.example > .env.local
  log ".env.local 생성됨 — 비밀번호 채우기 필요"
fi
ok "server/main_service 준비 완료"

# 3. PyQt
log "[2/3] ui/pyqt/factory_operator venv + 의존성"
cd "$ROOT/ui/pyqt/factory_operator"
PY=$(command -v python3.12 || command -v python3.11 || command -v python3)
[ -d .venv ] || $PY -m venv .venv
"./.venv/bin/pip" install --upgrade pip --quiet
"./.venv/bin/pip" install -r requirements.txt --quiet
[ -f .env.local ] || cp .env.example .env.local
ok "ui/pyqt/factory_operator 준비 완료"

# 4. Web
log "[3/3] ui/web npm install"
cd "$ROOT/ui/web"
npm install --silent --no-audit --no-fund
[ -f .env.local ] || cp .env.example .env.local
ok "ui/web 준비 완료"

log "[4/4] gRPC proto 스텁 코드 생성"
cd "$ROOT"
./scripts/gen_proto.sh
ok "gRPC proto 스텁 코드 생성 완료"

log "완료. 다음 단계:"
echo "  1) server/main_service/.env.local 의 DATABASE_URL 비밀번호/엔드포인트 입력"
echo "     - sslrootcert 는 $ROOT/global-bundle.pem 사용"
echo "  2) ./scripts/run-all.sh  (또는 개별 ./scripts/run-{backend,pyqt,web}.sh)"
