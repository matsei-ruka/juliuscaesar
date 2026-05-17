"""Tests for the phase classifier."""

from supervisor.phases import classify


def test_no_stderr_returns_starting():
    result = classify("", has_stderr=False)
    assert result.phase == "starting"
    assert result.emoji == "🟢"


def test_idle_when_mtime_stale():
    result = classify("some output", mtime_age_seconds=35.0, has_stderr=True)
    assert result.phase == "idle"
    assert result.emoji == "⏸️"


def test_idle_threshold_is_30s():
    # 30s exactly → idle; 29s → not idle (falls through to thinking/match)
    assert classify("x", mtime_age_seconds=30.1, has_stderr=True).phase == "idle"
    assert classify("x", mtime_age_seconds=29.9, has_stderr=True).phase != "idle"


def test_read_keyword_matches():
    result = classify("Read(/some/file.py) completed", has_stderr=True, mtime_age_seconds=2.0)
    assert result.phase == "reading"


def test_glob_keyword_matches_reading():
    result = classify("Glob(**/*.py) done", has_stderr=True, mtime_age_seconds=2.0)
    assert result.phase == "reading"


def test_bash_keyword_matches_coding():
    result = classify("Bash(ls -la) running", has_stderr=True, mtime_age_seconds=2.0)
    assert result.phase == "coding"


def test_edit_keyword_matches_coding():
    result = classify("Edit(file.py) writing 20 lines", has_stderr=True, mtime_age_seconds=2.0)
    assert result.phase == "coding"


def test_web_search_matches_web_research():
    result = classify("WebSearch(query) fetching results", has_stderr=True, mtime_age_seconds=2.0)
    assert result.phase == "web_research"


def test_grep_matches_searching():
    result = classify("Grep(pattern) searching", has_stderr=True, mtime_age_seconds=2.0)
    assert result.phase == "searching"


def test_most_recent_keyword_wins():
    # Edit appears after Grep in tail → coding wins
    tail = "Grep(pattern) found 3 files\nEdit(result.py) writing output"
    result = classify(tail, has_stderr=True, mtime_age_seconds=2.0)
    assert result.phase == "coding"


def test_earlier_keyword_loses():
    # Read appears before Bash → coding wins
    tail = "Read(file.py)\nthen Bash(chmod +x script.sh)"
    result = classify(tail, has_stderr=True, mtime_age_seconds=2.0)
    assert result.phase == "coding"


def test_no_match_returns_thinking():
    result = classify("lots of plain text without any known keywords", has_stderr=True, mtime_age_seconds=2.0)
    assert result.phase == "thinking"
    assert result.emoji == "💭"


def test_label_for_it():
    result = classify("", has_stderr=False)  # starting
    assert result.label_for("it") == "avvio"


def test_label_for_en():
    result = classify("", has_stderr=False)  # starting
    assert result.label_for("en") == "starting"


def test_label_unknown_lang_falls_back_to_en():
    result = classify("Edit(x)", has_stderr=True, mtime_age_seconds=0.5)
    label = result.label_for("de")  # not in table
    assert label  # non-empty


def test_phase_result_has_emoji():
    for tail, expected_phase in [
        ("Bash(x)", "coding"),
        ("Read(x)", "reading"),
        ("Grep(x) searching for y", "searching"),
    ]:
        result = classify(tail, has_stderr=True, mtime_age_seconds=1.0)
        assert result.phase == expected_phase
        assert result.emoji  # non-empty
