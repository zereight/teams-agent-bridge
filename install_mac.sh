#!/usr/bin/env bash
# Teams 에이전트 브리지 - macOS 설치 스크립트
# 하는 일: ~/teams-agent-bridge/.venv 가상환경 생성, requirements 설치,
#          config.yaml 이 없으면 config.example.yaml 에서 복사, 다음 단계 안내 출력.
# 안 하는 일: 브리지 실행 안 함, 시스템 설정 변경 안 함, sudo 필요 없음.
set -euo pipefail

APP_DIR="$HOME/teams-agent-bridge"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> 소스: $SRC_DIR"
echo "==> 설치 위치: $APP_DIR"

# 소스가 설치 위치와 다르면 파일을 복사 (venv/data 제외)
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  mkdir -p "$APP_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
          --exclude '.pytest_cache' "$SRC_DIR"/ "$APP_DIR"/
  else
    cp -R "$SRC_DIR"/. "$APP_DIR"/
    rm -rf "$APP_DIR/.venv" "$APP_DIR/data"
  fi
fi
cd "$APP_DIR"

# Python 3 확인
if ! command -v python3 >/dev/null 2>&1; then
  echo "!! python3 가 없어요. https://www.python.org 에서 Python 3.11+ 를 설치하거나 'brew install python' 후 다시 실행하세요." >&2
  exit 1
fi
echo "==> $(python3 --version)"

echo "==> 가상환경 만드는 중 (.venv)"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
echo "==> requirements 설치 중 (잠시 걸려요)"
pip install -r requirements.txt

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "==> config.example.yaml -> config.yaml 복사함"
else
  echo "==> config.yaml 이 이미 있어서 건드리지 않았어요"
fi

cat <<NEXT

====================  설치 끝! 다음 단계  ====================
설치 폴더: $APP_DIR

1) 권한 켜기 (시스템 설정에서 직접 해야 해요, 스크립트가 못 바꿔요)
   - 시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용(Accessibility)
     -> 이 브리지를 '실행하는 터미널 앱'(터미널/iTerm/VS Code 등)을 추가하고 켜기
   - (비전 백엔드를 쓸 때만) 개인정보 보호 및 보안 > 화면 기록(Screen Recording)
     -> 같은 터미널 앱 켜기

2) Teams 로그인 & 나와의 채팅 열기
   - 새 Teams 를 열고 '나와의 채팅'을 띄워 둔 뒤 아무 메시지나 하나 보내 두세요.

3) 코딩 에이전트 로그인 (회사 엔터프라이즈 계정)
   - Claude Code:  claude auth login   (확인: claude auth status)
   - Codex:        codex login         (확인: codex login status)

4) config.yaml 열어서 맞추기
   - agent.workdir : 에이전트가 일할 프로젝트 폴더 (예: /Users/me/work/proj)
   - ax.bundle_id  : 새 Teams = com.microsoft.teams2 (클래식 = com.microsoft.teams)
   - self_chat.title_regex, ax.message_list 등 (README '선택자 찾기' 참고)

5) 선택자 확인 (AX 트리 보기)
   source .venv/bin/activate
   python bridge.py --config config.yaml --dump-tree        | less
   python bridge.py --config config.yaml --show-messages

6) 먼저 dry-run 으로 테스트 후 진짜 실행
   python bridge.py --config config.yaml --dry-run
   ./run_mac.sh      (또는) python bridge.py --config config.yaml
===============================================================
NEXT
