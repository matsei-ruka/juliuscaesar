"""Tests for snapshot module helper functions."""

from supervisor.snapshot import (
    _brain_map_from_log,
    _detect_language,
    _extract_int,
    _extract_token,
    _pid_map_from_log,
)


# --- _extract_int / _extract_token ---

def test_extract_int_basic():
    assert _extract_int("event=42 brain=claude", "event=") == 42


def test_extract_int_missing():
    assert _extract_int("brain=claude model=sonnet", "event=") is None


def test_extract_int_trailing_comma():
    assert _extract_int("event=42,brain=claude", "event=") == 42


def test_extract_token_basic():
    assert _extract_token("brain=claude model=sonnet", "brain=") == "claude"


def test_extract_token_missing():
    assert _extract_token("event=1", "brain=") == ""


def test_extract_token_with_colon():
    assert _extract_token("brain=claude:sonnet model=x", "brain=") == "claude:sonnet"


# --- _brain_map_from_log ---

def test_brain_map_parses_spawn_lines():
    lines = [
        '{"ts":"t","msg":"adapter spawn event=10 brain=claude model=sonnet pid=1234 ..."}',
        '{"ts":"t","msg":"adapter spawn event=11 brain=pi model=deepseek-v4-pro pid=5678"}',
    ]
    m = _brain_map_from_log(lines)
    assert m[10] == ("claude", "sonnet")
    assert m[11] == ("pi", "deepseek-v4-pro")


def test_brain_map_last_wins():
    # Same event_id appears twice — last line wins
    lines = [
        "adapter spawn event=10 brain=claude model=sonnet pid=1",
        "adapter spawn event=10 brain=pi model=flash pid=2",
    ]
    m = _brain_map_from_log(lines)
    assert m[10][0] == "pi"


def test_brain_map_ignores_no_event():
    lines = ["random log line without event id"]
    assert _brain_map_from_log(lines) == {}


# --- _pid_map_from_log ---

def test_pid_map_parses_adapter_spawn():
    lines = [
        "adapter spawn event=5 brain=claude pid=9999 model=sonnet",
    ]
    m = _pid_map_from_log(lines)
    assert m[5] == 9999


def test_pid_map_ignores_non_spawn_lines():
    lines = [
        "dispatch begin event=5 brain=claude",
        "event failed event=5",
    ]
    assert _pid_map_from_log(lines) == {}


# --- _detect_language ---

def test_detect_language_from_meta():
    assert _detect_language({"language": "it"}, "") == "it"
    assert _detect_language({"language": "en"}, "") == "en"


def test_detect_language_meta_takes_precedence():
    # Even if content looks Italian, meta wins
    assert _detect_language({"language": "en"}, "ciao come stai di questa cosa") == "en"


def test_detect_language_italian_heuristic():
    text = "ciao, vorrei sapere di più per il progetto con la deadline"
    assert _detect_language({}, text) == "it"


def test_detect_language_english_default():
    assert _detect_language({}, "hello please help me fix this bug") == "en"


def test_detect_language_empty_content():
    assert _detect_language({}, "") == "en"
