import os

import pytest

import safety
from config import DEFAULTS, _reject_inline_secrets

PAT = safety.compile_patterns(DEFAULTS["safety"]["dangerous_patterns"])
APPROVE = DEFAULTS["safety"]["approval_words"]
CANCEL = DEFAULTS["safety"]["cancel_words"]
PREFIXES = DEFAULTS["bot_prefixes_detect"]


@pytest.mark.parametrize("text", [
    "(b) 완료했어요", "  (b)시작했어요", "[b] 결과", "🍺 완료 (claude, 3초)",
    "(b)\u00a0공백 변형",
])
def test_bot_messages_detected(text):
    assert safety.is_bot_message(text, PREFIXES)


@pytest.mark.parametrize("text", ["테스트 돌려줘", "b 로 시작", "(bb) 아님", "/status"])
def test_user_messages_not_bot(text):
    assert not safety.is_bot_message(text, PREFIXES)


def test_recent_sent_snippet_blocks_mangled_prefix():
    sent = ["완료 (claude, 42초, exit 0) 로그인 테스트"]
    # Teams dropped the "(b)" prefix entirely
    assert safety.is_bot_message("완료 (claude, 42초, exit 0) 로그인 테스트 전부 통과", PREFIXES, sent)
    assert not safety.is_bot_message("로그인 테스트 다시 해줘", PREFIXES, sent)


def test_short_snippets_ignored():
    assert not safety.is_bot_message("ok 해줘", PREFIXES, ["ok"])


@pytest.mark.parametrize("text,cmd", [
    ("/status", "/status"), ("/STOP", "/stop"), (" /start now", "/start"),
    ("/help", "/help"), ("/rm -rf", None), ("status", None),
])
def test_parse_builtin(text, cmd):
    assert safety.parse_builtin(text) == cmd


@pytest.mark.parametrize("text", [
    "rm -rf build", "git push origin main", "Deploy to staging", "DROP TABLE users",
    "prod DB 확인해줘", "production 로그 봐줘", "운영 서버 재시작", "배포해줘", "로그 파일 삭제해줘",
    "main에 푸시해", "git reset --hard HEAD~1", "브랜치 병합해줘", "push --force", "임시파일 지워줘",
    "release 노트 만들고 publish", "DB 마이그레이션 실행",
])
def test_dangerous(text):
    assert safety.is_dangerous(text, PAT), text


@pytest.mark.parametrize("text", [
    "로그인 테스트 돌리고 결과 알려줘", "README 요약해줘", "pushover 라이브러리 설명", "products 페이지 버그 찾아줘",
    "단위 테스트 추가해줘", "코드 포맷 맞춰줘", "deletion 이라는 단어 설명",
])
def test_not_dangerous(text):
    assert not safety.is_dangerous(text, PAT), (text, safety.dangerous_matches(text, PAT))


@pytest.mark.parametrize("text", ["ㅇㅇ", "ㅇㅇ!", " yes ", "YES", "실행", "ok."])
def test_approval(text):
    assert safety.is_approval(text, APPROVE)


@pytest.mark.parametrize("text", ["ㅇㅇ 근데 잠깐", "응 아니", "yes please delete more", ""])
def test_not_approval(text):
    assert not safety.is_approval(text, APPROVE)


def test_cancel():
    assert safety.is_cancel("취소", CANCEL)
    assert safety.is_cancel("No", CANCEL)
    assert not safety.is_cancel("취소하지 말고 계속", CANCEL)


def test_rate_limiter_repeat_and_volume():
    now = [1000.0]
    rl = safety.RateLimiter(max_per_hour=5, repeat_limit=2, repeat_window_sec=600, clock=lambda: now[0])
    assert rl.check("a")[0]
    assert rl.check("a")[0]
    ok, why = rl.check("a")
    assert not ok and "같은 명령" in why
    now[0] += 601                       # repeat window passed
    assert rl.check("a")[0]
    assert rl.check("b")[0]
    assert rl.check("c")[0]
    ok, why = rl.check("d")             # 5 accepted already in the hour
    assert not ok and "1시간" in why
    now[0] += 3601
    assert rl.check("d")[0]


def test_check_workdir(tmp_path):
    root = tmp_path / "work"
    proj = root / "proj"
    proj.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    assert safety.check_workdir(str(proj), [str(root)]) == os.path.normcase(os.path.realpath(proj))
    assert safety.check_workdir(str(root), [str(root)])
    with pytest.raises(ValueError):
        safety.check_workdir(str(other), [str(root)])
    with pytest.raises(ValueError):  # prefix trick: /work-evil is not inside /work
        evil = tmp_path / "work-evil"
        evil.mkdir()
        safety.check_workdir(str(evil), [str(root)])
    with pytest.raises(ValueError):
        safety.check_workdir("", [])
    with pytest.raises(ValueError):
        safety.check_workdir(str(tmp_path / "missing"), [])


def test_check_workdir_symlink_escape(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")
    with pytest.raises(ValueError):
        safety.check_workdir(str(link), [str(root)])


def test_truncate():
    assert safety.truncate("abc", 10) == ("abc", False)
    t, cut = safety.truncate("x" * 50, 10)
    assert cut and len(t) == 10 and t.endswith("…")


def test_redact():
    s = "key sk-abcdefghijklmnopqrstuvwxyz123 and password=hunter2 and ghp_" + "a" * 30
    r = safety.redact(s)
    assert "hunter2" not in r and "sk-abc" not in r and "ghp_aaa" not in r
    assert "[REDACTED]" in r


def test_config_rejects_inline_secrets():
    with pytest.raises(ValueError):
        _reject_inline_secrets({"vision": {"api_key": "sk-123"}})
    _reject_inline_secrets({"vision": {"api_key_env": "ANTHROPIC_API_KEY", "max_tokens": 2000}})
