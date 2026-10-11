import json

from backend_base import ChatMessage
from state import StateStore, base_fingerprint, find_new_start


def msgs(*texts, ts=""):
    return [ChatMessage(text=t, timestamp=ts) for t in texts]


def fps(*texts):
    return [base_fingerprint(t) for t in texts]


def test_find_new_start_basic():
    a, b, c, d, e, f = fps("a", "b", "c", "d", "e", "f")
    assert find_new_start([a, b, c, d], [c, d, e, f]) == 2      # scrolled + 2 new
    assert find_new_start([a, b, c, d], [a, b, c, d]) == 4      # nothing new
    assert find_new_start([a, b, c, d], [c, d]) == 2            # window shrank
    assert find_new_start([a, b], [a, b, c]) == 2               # window grew
    assert find_new_start([b, c], [a, b, c, d]) == 3            # more history loaded above
    assert find_new_start([a, b], [e, f]) is None               # no overlap -> resync
    assert find_new_start([], [a]) is None


def test_find_new_start_duplicates():
    x, y, ok = fps("x", "y", "ㅇㅇ")
    assert find_new_start([x, ok], [ok, ok]) == 1       # x scrolled off, new "ㅇㅇ"
    assert find_new_start([x, ok], [x, ok, ok]) == 2
    assert find_new_start([ok, x, ok], [ok, x, ok, y, ok]) == 3


def test_first_start_marks_history(tmp_path):
    st = StateStore(str(tmp_path / "s.json"))
    assert st.filter_new(msgs("old cmd 1", "old cmd 2")) is None
    assert st.last_event == "init"
    assert st.filter_new(msgs("old cmd 1", "old cmd 2")) == []


def test_new_messages_and_restart_persistence(tmp_path):
    p = str(tmp_path / "s.json")
    st = StateStore(p)
    st.filter_new(msgs("a", "b"))
    new = st.filter_new(msgs("a", "b", "c"))
    assert [m.text for m in new] == ["c"]
    # restart: new store object, same file -> nothing re-runs
    st2 = StateStore(p)
    assert st2.initialized
    assert st2.filter_new(msgs("a", "b", "c")) == []
    new = st2.filter_new(msgs("b", "c", "d"))
    assert [m.text for m in new] == ["d"]
    data = json.load(open(p, encoding="utf-8"))
    assert data["initialized"] and len(data["processed"]) >= 4


def test_repeated_identical_command_runs_again(tmp_path):
    st = StateStore(str(tmp_path / "s.json"))
    st.filter_new(msgs("테스트 돌려줘", "(b) 완료"))
    new = st.filter_new(msgs("테스트 돌려줘", "(b) 완료", "테스트 돌려줘"))
    assert [m.text for m in new] == ["테스트 돌려줘"]


def test_resync_reanchors_without_replaying_history(tmp_path):
    st = StateStore(str(tmp_path / "s.json"))
    st.filter_new(msgs("a", "b"))
    # user scrolled far up: totally different, UNTIMESTAMPED messages visible.
    # Anchor is lost -> resync, but old untimestamped history is NOT replayed.
    out = st.filter_new(msgs("ancient 1", "ancient 2"))
    assert out == [] and st.last_event == "resync"
    # scrolled back down; old window lines up with... one new message to run
    out = st.filter_new(msgs("ancient 2", "x"))
    assert [m.text for m in out] == ["x"]


def test_resync_surfaces_new_timestamped_command(tmp_path):
    # a command typed in the SAME poll as a resync must still be processed if it
    # is new (trailing, timestamped, never processed), rather than being swallowed.
    st = StateStore(str(tmp_path / "s.json"))
    st.filter_new([ChatMessage("old", "13:00")])               # init
    st.data["seen_tail"] = [base_fingerprint("ZZZ")]           # force anchor loss
    out = st.filter_new([ChatMessage("문맥 메모", "13:04"), ChatMessage("/status", "13:05")])
    assert st.last_event == "resync"
    assert "/status" in [m.text for m in out]


def test_resync_does_not_replay_scrolled_in_history(tmp_path):
    # old messages that scroll into view (above an already-processed one) are NOT
    # replayed, even though the anchor was lost (trailing run stops at 'recent').
    st = StateStore(str(tmp_path / "s.json"))
    st.filter_new([ChatMessage("recent", "13:00")])            # init: 'recent' processed
    st.data["seen_tail"] = [base_fingerprint("ZZZ")]           # force anchor loss
    out = st.filter_new([ChatMessage("ancient", "09:00"),      # scrolled in above
                         ChatMessage("recent", "13:00")])       # processed -> tail stops here
    assert st.last_event == "resync" and out == []


def test_timestamp_guard(tmp_path):
    st = StateStore(str(tmp_path / "s.json"))
    first = [ChatMessage("a", "13:00"), ChatMessage("b", "13:01")]
    st.filter_new(first)
    out = st.filter_new([ChatMessage("a", "13:00"), ChatMessage("b", "13:01"), ChatMessage("c", "13:02")])
    assert [m.text for m in out] == ["c"]
    # same message re-rendered with same timestamp but alignment confused -> guard prevents rerun
    st.data["seen_tail"] = [base_fingerprint("zzz")] + [base_fingerprint("a", "13:00")]
    out = st.filter_new([ChatMessage("a", "13:00"), ChatMessage("c", "13:02")])
    assert out == []


def test_whitespace_normalisation(tmp_path):
    st = StateStore(str(tmp_path / "s.json"))
    st.filter_new(msgs("hello  world"))
    assert st.filter_new(msgs("hello world\u200b")) == []


def test_empty_read_keeps_state(tmp_path):
    st = StateStore(str(tmp_path / "s.json"))
    st.filter_new(msgs("a"))
    assert st.filter_new([]) == []
    assert [m.text for m in st.filter_new(msgs("a", "b"))] == ["b"]


def test_paused_persists(tmp_path):
    p = str(tmp_path / "s.json")
    st = StateStore(p)
    st.paused = True
    assert StateStore(p).paused
