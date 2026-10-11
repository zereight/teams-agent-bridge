#!/usr/bin/env bash
# 브리지를 포그라운드로 실행. Ctrl+C 로 종료.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
if [ ! -d .venv ]; then
  echo "!! .venv 가 없어요. 먼저 ./install_mac.sh 를 실행하세요." >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
exec python bridge.py --config "${1:-config.yaml}"
