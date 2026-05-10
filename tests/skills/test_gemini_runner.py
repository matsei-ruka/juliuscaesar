"""Unit tests for `lib/skills/gemini_deep_research/runner.py`.

Covers the pure helpers (prompt builder, source parser, error mappers,
report renderer, log redaction) plus the end-to-end `run_query` flow with
a fake Playwright session injected via `browser_factory`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from skills.gemini_deep_research import errors, selectors
from skills.gemini_deep_research.runner import (
    NAV_MODEL_ENV,
    RunInputs,
    build_prompt,
    map_text_to_error,
    map_url_to_error,
    parse_sources,
    redact,
    render_report,
    run_query,
    write_meta,
)


# --- pure helpers -----------------------------------------------------------


def test_build_prompt_normalises_whitespace_and_pads_short_queries() -> None:
    assert build_prompt("ping").startswith("Run a thorough deep research")
    assert build_prompt("   compare\teSIM market UAE\nvs\tKSA  2025  ") == (
        "compare eSIM market UAE vs KSA 2025"
    )


def test_parse_sources_handles_markdown_links_and_dedups() -> None:
    text = (
        "1. [Statista UAE eSIM 2025](https://www.statista.com/x).\n"
        "2. [Reuters](https://reuters.com/article/y);\n"
        "3. [Statista UAE eSIM 2025](https://www.statista.com/x)\n"
    )
    out = parse_sources(text)
    urls = [s["url"] for s in out]
    assert urls == ["https://www.statista.com/x", "https://reuters.com/article/y"]
    assert out[0]["domain"] == "www.statista.com"


def test_parse_sources_falls_back_to_bare_urls() -> None:
    text = "see https://example.com/foo and https://example.com/bar."
    out = parse_sources(text)
    assert [s["url"] for s in out] == ["https://example.com/foo", "https://example.com/bar"]


def test_map_url_to_error_signals_auth_required() -> None:
    assert map_url_to_error("https://accounts.google.com/signin?continue=...") == errors.EXIT_AUTH_REQUIRED
    assert map_url_to_error("https://gemini.google.com/app") is None


def test_map_text_to_error_covers_captcha_and_quota() -> None:
    assert map_text_to_error("Please verify you are not a robot") == errors.EXIT_CAPTCHA
    assert map_text_to_error("daily quota reached, try again later") == errors.EXIT_QUOTA
    assert map_text_to_error("regular page") is None


def test_redact_strips_cookie_and_authorization_headers() -> None:
    blob = "Cookie: __Secure-1PSIDTS=abc123; SID=xyz\nAuthorization: Bearer abc.def"
    out = redact(blob)
    assert "abc123" not in out
    assert "[redacted]" in out


def test_render_report_includes_frontmatter_and_sources() -> None:
    md = render_report(
        query="Q",
        job_id="job-1",
        started_at="2026-05-10T14:00:00Z",
        finished_at="2026-05-10T14:05:00Z",
        duration_seconds=300,
        body_markdown="# Heading\n\nbody.",
        sources=[{"title": "T", "url": "https://x.com/p", "domain": "x.com"}],
        exit_code=0,
        title="Research title",
    )
    assert md.startswith("---\n")
    assert 'query: "Q"' in md
    assert "exit_code: 0" in md
    assert "## Sources" in md
    assert "[T](https://x.com/p) — x.com" in md
    assert "# Research title" in md


def test_write_meta_writes_json(tmp_path: Path) -> None:
    path = write_meta(
        tmp_path,
        job_id="j1",
        query="hello",
        exit_code=0,
        label="ok",
        message="ok",
        started_at="2026-05-10T14:00:00Z",
        finished_at="2026-05-10T14:01:00Z",
        duration_seconds=60.0,
        last_url="https://gemini.google.com/app",
        sources_count=3,
    )
    payload = json.loads(path.read_text())
    assert payload["job_id"] == "j1"
    assert payload["sources_count"] == 3
    assert payload["ui_version"] == selectors.UI_VERSION


# --- run_query with fake browser -------------------------------------------


class _FakeLocator:
    def __init__(self, *, fail_wait: bool = False, body: str = "", text: str = "") -> None:
        self._fail_wait = fail_wait
        self._body = body
        self._text = text
        self.click_calls = 0

    def wait_for(self, **kwargs: Any) -> None:
        if self._fail_wait:
            raise RuntimeError("not visible")

    def click(self, **kwargs: Any) -> None:
        self.click_calls += 1

    def fill(self, value: str, **kwargs: Any) -> None:
        self._typed = value

    def inner_text(self, **kwargs: Any) -> str:
        return self._text or self._body

    @property
    def first(self) -> "_FakeLocator":
        return self


class _FakeKeyboard:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def press(self, key: str) -> None:
        self.events.append(("press", key))

    def type(self, text: str, **kwargs: Any) -> None:
        self.events.append(("type", text))


class _FakePage:
    def __init__(
        self,
        *,
        body_text: str,
        sources_text: str,
        completion_text: str,
        after_goto_url: str | None = None,
    ) -> None:
        self.url = "https://gemini.google.com/app"
        self.keyboard = _FakeKeyboard()
        self._body_text = body_text
        self._sources_text = sources_text
        self._completion_text = completion_text
        self._after_goto_url = after_goto_url
        self.screenshot_path: str | None = None

    def goto(self, url: str, **kwargs: Any) -> None:
        self.url = self._after_goto_url or url

    def locator(self, css: str) -> _FakeLocator:
        if "h1" in css:
            return _FakeLocator(text="Research title")
        if "main" in css:
            return _FakeLocator(text=self._body_text)
        if "Sources" in css or "sources" in css:
            return _FakeLocator(text=self._sources_text)
        if "Export" in css or "export" in css:
            return _FakeLocator()
        if "Start research" in css or "Begin research" in css or "start-research" in css:
            return _FakeLocator()
        if "Deep Research" in css or "deep-research" in css:
            return _FakeLocator()
        if "model" in css or "Switch" in css:
            return _FakeLocator()
        if "textbox" in css or "prompt" in css or "chat-input" in css:
            return _FakeLocator()
        return _FakeLocator()

    def content(self) -> str:
        return self._completion_text

    def screenshot(self, path: str, **kwargs: Any) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        self.screenshot_path = path


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.closed = False

    def new_page(self) -> _FakePage:
        return self._page

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    body = "Gemini-rendered Markdown report body."
    sources = "1. [Statista UAE](https://statista.com/uae)\n2. [Reuters](https://reuters.com/x)\n"
    completion = "Research complete. Export available."
    page = _FakePage(body_text=body, sources_text=sources, completion_text=completion)
    ctx = _FakeContext(page)

    # Point the profile dir at a tmp dir so the lock file stays sandboxed.
    monkeypatch.setenv("JC_RESEARCH_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.delenv("JC_RESEARCH_DISABLED", raising=False)

    def factory(*, headed: bool) -> _FakeContext:
        return ctx

    return factory, page, ctx


def test_run_query_happy_path_writes_report_meta_screenshot(
    tmp_path: Path,
    fake_factory: tuple[Any, _FakePage, _FakeContext],
) -> None:
    factory, _page, ctx = fake_factory
    out = tmp_path / "out"
    inputs = RunInputs(
        query="Compare eSIM market UAE vs KSA 2025",
        out_dir=out,
        job_id="job-A",
        max_wait_seconds=5,
    )
    result = run_query(inputs, browser_factory=factory)
    assert result.exit_code == 0, result.message
    assert result.report_path is not None and result.report_path.exists()
    assert result.meta_path is not None and result.meta_path.exists()
    assert result.sources_count == 2
    payload = json.loads(result.meta_path.read_text())
    assert payload["exit_code"] == 0
    assert payload["sources_count"] == 2
    assert ctx.closed is True


def test_run_query_auth_required_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = _FakePage(
        body_text="",
        sources_text="",
        completion_text="",
        after_goto_url="https://accounts.google.com/signin",
    )
    ctx = _FakeContext(page)
    monkeypatch.setenv("JC_RESEARCH_PROFILE_DIR", str(tmp_path / "profile"))

    def factory(*, headed: bool) -> _FakeContext:
        return ctx

    inputs = RunInputs(query="hello", out_dir=tmp_path / "out", job_id="j2", max_wait_seconds=5)
    result = run_query(inputs, browser_factory=factory)
    assert result.exit_code == errors.EXIT_AUTH_REQUIRED
    assert result.label == "auth_required"
    assert result.meta_path is not None and result.meta_path.exists()
    payload = json.loads(result.meta_path.read_text())
    assert payload["exit_code"] == errors.EXIT_AUTH_REQUIRED


def test_run_query_disabled_env_returns_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JC_RESEARCH_DISABLED", "1")
    monkeypatch.setenv("JC_RESEARCH_PROFILE_DIR", str(tmp_path / "profile"))

    def factory(*, headed: bool) -> _FakeContext:
        raise AssertionError("factory should not be called when disabled")

    inputs = RunInputs(query="x", out_dir=tmp_path / "out", job_id="j3", max_wait_seconds=5)
    result = run_query(inputs, browser_factory=factory)
    assert result.exit_code == errors.EXIT_INVALID_INPUT


def test_nav_model_env_constant_exposed() -> None:
    assert NAV_MODEL_ENV == "JC_RESEARCH_NAV_MODEL"
