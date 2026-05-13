#!/usr/bin/env bash
# Next.js Web :3001 실행.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/ui/web"

[ -d node_modules ] || { echo "✗ node_modules 없음. ./scripts/setup.sh 먼저 실행."; exit 1; }

# nvm이 설치돼 있으면 로드 (gnome-terminal 등 새 셸에서 PATH 미적용 상황 대비)
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

PORT="${PORT:-3001}"
echo "→ Next.js dev on http://localhost:$PORT"
exec npm run dev -- --port "$PORT"
