"""Per-turn STYLE voice anchor injection."""

from __future__ import annotations

from pathlib import Path

from gateway import config as gateway_config
from gateway import context
from gateway.brains.claude import ClaudeBrain
from gateway.brains.opencode import OpencodeBrain
from gateway.queue import Event


def _event(content: str = "please reply") -> Event:
    return Event(
        id=1,
        source="telegram",
        source_message_id="m1",
        user_id="u1",
        conversation_id="c1",
        content=content,
        meta="{}",
        status="queued",
        received_at="2026-05-09T00:00:00Z",
        available_at="2026-05-09T00:00:00Z",
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


def _instance(tmp_path: Path, *, style: str | None = None) -> Path:
    instance = tmp_path / "instance"
    (instance / "ops").mkdir(parents=True)
    (instance / "ops" / "gateway.yaml").write_text("timezone: Asia/Dubai\n", encoding="utf-8")
    l1 = instance / "memory" / "L1"
    l1.mkdir(parents=True)
    (l1 / "IDENTITY.md").write_text("identity", encoding="utf-8")
    if style is not None:
        (l1 / "STYLE.md").write_text(style, encoding="utf-8")
    gateway_config.clear_config_cache()
    context.clear_cache()
    return instance


STYLE = """
# Voice anchor

> Never mirror. Stay crisp and a little dangerous.

## Caveman

caveman: disabled
""".lstrip()


def test_non_claude_prompt_injects_voice_anchor(tmp_path: Path) -> None:
    instance = _instance(tmp_path, style=STYLE)

    text = OpencodeBrain(instance).prompt_for_event(_event())

    assert "[Voice: Never mirror. Stay crisp and a little dangerous.]" in text
    assert text.index("[Voice:") < text.index("please reply")


def test_claude_prompt_injects_anchor_after_clock_before_body(tmp_path: Path) -> None:
    instance = _instance(tmp_path, style=STYLE)

    text = ClaudeBrain(instance).prompt_for_event(_event())
    tail = text[text.index("# User message") :]

    assert "[Current time:" in tail
    assert "[Voice: Never mirror. Stay crisp and a little dangerous.]" in tail
    assert tail.index("[Current time:") < tail.index("[Voice:")
    assert tail.index("[Voice:") < tail.index("please reply")


def test_no_style_means_no_voice_anchor(tmp_path: Path) -> None:
    instance = _instance(tmp_path)

    assert "[Voice:" not in OpencodeBrain(instance).prompt_for_event(_event())
    assert "[Voice:" not in ClaudeBrain(instance).prompt_for_event(_event())
