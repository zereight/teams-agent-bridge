#!/usr/bin/env bash
# Fail if personal / company-specific strings leak into the repo tree.
# Run from anywhere:  scripts/check_no_secrets.sh [ROOT]
# Add your own strings via EXTRA_PATTERNS='foo|bar' scripts/check_no_secrets.sh
set -euo pipefail
ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

# Personal/company strings that must never ship (case-insensitive, ERE).
PATTERNS='tao\.exe|/Users/tao|Tao Kim|\(Tao\)|김정혁|정혁|mobile-app-workspace|bankx|kakaobank|react native|\.nvm/versions/node/v[0-9]|\.bun/bin/codex|plugins/cache/pstack-claude'
if [ -n "${EXTRA_PATTERNS:-}" ]; then
  PATTERNS="$PATTERNS|$EXTRA_PATTERNS"
fi

# Never scan: VCS, envs, runtime data, caches, private dumps, this script.
EXCLUDES=(
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=venv
  --exclude-dir=data --exclude-dir=worktrees --exclude-dir=__pycache__
  --exclude-dir=.pytest_cache --exclude-dir=node_modules
  --exclude=check_no_secrets.sh --exclude=config.yaml
  --exclude='axtree*.txt' --exclude='*.zip' --exclude='*.bak'
)

if hits=$(grep -rIniE "${EXCLUDES[@]}" -- "$PATTERNS" . 2>/dev/null); then
  echo "✗ check_no_secrets: personal/company strings found:" >&2
  echo "$hits" >&2
  exit 1
fi
echo "✓ check_no_secrets: no personal/company strings found in $ROOT"
