"""Browser-driving runner for a single deep-research query.

This module is the only place that imports `playwright` / `browser_use`.
Imports are lazy so:
  * unit tests can mock the runner without the browser deps installed
  * `import lib.skills.gemini_deep_research` stays cheap

The deterministic-first flow tries our pinned selectors; on miss it hands
the same step to a browser-use Agent with a tight goal + 30s budget.
Failure of both → `DeepResearchError(EXIT_SELECTORS_FAILED)`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import selectors
from .auth import acquire_lock, ensure_profile_dir
from .errors import (
    EXIT_AUTH_REQUIRED,
    EXIT_BROWSER_CRASH,
    EXIT_CAPTCHA,
    EXIT_DEEP_RESEARCH_UNAVAILABLE,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_QUOTA,
    EXIT_SELECTORS_FAILED,
    DeepResearchError,
)


GEMINI_URL = "https://gemini.google.com/app"

LOG_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^\s*(cookie|set-cookie|authorization)\s*:\s*.+$"),
    re.compile(r"(?i)(__Secure-[A-Za-z0-9_-]+|SID|HSID|SSID|APISID|SAPISID)=[^\s;]+"),
)

NAV_MODEL_ENV = "JC_RESEARCH_NAV_MODEL"
DEFAULT_NAV_MODEL = "openrouter/openai/gpt-4o-mini"
DISABLED_ENV = "JC_RESEARCH_DISABLED"


@dataclass
class RunInputs:
    query: str
    out_dir: Path
    job_id: str
    max_wait_seconds: int = 900
    headed: bool = False
    nav_model: str | None = None


@dataclass
class RunResult:
    exit_code: int
    message: str
    report_path: Path | None = None
    meta_path: Path | None = None
    screenshot_path: Path | None = None
    duration_seconds: float = 0.0
    sources_count: int = 0
    last_url: str | None = None
    label: str = "ok"
    extra: dict[str, Any] = field(default_factory=dict)


def assert_inputs(inputs: RunInputs) -> None:
    if os.environ.get(DISABLED_ENV):
        raise DeepResearchError(
            EXIT_INVALID_INPUT,
            f"{DISABLED_ENV} is set in the environment; this instance cannot spend the subscription.",
        )
    if not inputs.query or not inputs.query.strip():
        raise DeepResearchError(EXIT_INVALID_INPUT, "query is empty")
    if inputs.max_wait_seconds <= 0:
        raise DeepResearchError(EXIT_INVALID_INPUT, "--max-wait must be positive")


def build_prompt(query: str) -> str:
    """Wrap the user's query for Deep Research mode.

    Kept tiny on purpose: Gemini's Deep Research planner is the smart
    bit, we just normalise whitespace and add a polite preamble so very
    short queries ("ping") don't trigger Gemini's "did you mean" path.
    """
    cleaned = re.sub(r"\s+", " ", query).strip()
    if len(cleaned) < 12:
        return f"Run a thorough deep research on the following question and return findings with sources: {cleaned}"
    return cleaned


def redact(text: str) -> str:
    out = text
    for pattern in LOG_REDACT_PATTERNS:
        out = pattern.sub("[redacted]", out)
    return out


def parse_sources(sidebar_text: str) -> list[dict[str, str]]:
    """Pull `(title, url, domain)` triples out of the sources sidebar.

    Robust to bullet/no-bullet, nbsp, accidental trailing punctuation.
    Used by `extract_sources()` in the live flow and by unit tests with a
    fixture sidebar.
    """
    out: list[dict[str, str]] = []
    md_link = re.compile(r"\[([^\]]{1,200})\]\((https?://[^\s)]+)\)")
    for match in md_link.finditer(sidebar_text):
        title = match.group(1).strip()
        url = match.group(2).strip().rstrip(".,);")
        domain = url.split("/")[2] if "://" in url and len(url.split("/")) > 2 else ""
        out.append({"title": title, "url": url, "domain": domain})
    if out:
        return _dedupe_sources(out)

    bare = re.compile(r"https?://[^\s<>\")']+", re.UNICODE)
    for match in bare.finditer(sidebar_text):
        url = match.group(0).rstrip(".,);")
        domain = url.split("/")[2] if "://" in url and len(url.split("/")) > 2 else ""
        out.append({"title": domain or url, "url": url, "domain": domain})
    return _dedupe_sources(out)


def _dedupe_sources(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in items:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        out.append(item)
    return out


def map_url_to_error(url: str) -> int | None:
    lower = (url or "").lower()
    for frag in selectors.SIGNED_OUT_URL_FRAGMENTS:
        if frag in lower:
            return EXIT_AUTH_REQUIRED
    return None


def map_text_to_error(text: str) -> int | None:
    lower = (text or "").lower()
    for frag in selectors.CAPTCHA_FRAGMENTS:
        if frag in lower:
            return EXIT_CAPTCHA
    for frag in selectors.QUOTA_FRAGMENTS:
        if frag in lower:
            return EXIT_QUOTA
    return None


def render_report(
    *,
    query: str,
    job_id: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    body_markdown: str,
    sources: list[dict[str, str]],
    exit_code: int,
    title: str | None = None,
) -> str:
    front = textwrap.dedent(
        f"""\
        ---
        query: {json.dumps(query)}
        job_id: {job_id}
        started: {started_at}
        finished: {finished_at}
        duration_seconds: {int(duration_seconds)}
        model: gemini-deep-research
        sources_count: {len(sources)}
        exit_code: {exit_code}
        ---
        """
    )
    head = f"# {title.strip()}\n\n" if title else ""
    body = body_markdown.strip() + "\n"
    if sources:
        lines = ["", "## Sources", ""]
        for idx, src in enumerate(sources, start=1):
            url = src.get("url", "")
            label = src.get("title") or src.get("domain") or url
            domain = src.get("domain", "")
            suffix = f" — {domain}" if domain else ""
            lines.append(f"{idx}. [{label}]({url}){suffix}")
        body += "\n" + "\n".join(lines) + "\n"
    return front + head + body


def write_meta(
    out_dir: Path,
    *,
    job_id: str,
    query: str,
    exit_code: int,
    label: str,
    message: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    last_url: str | None,
    sources_count: int,
    backend: str = "gemini",
    extra: dict[str, Any] | None = None,
) -> Path:
    meta = {
        "job_id": job_id,
        "query": query,
        "backend": backend,
        "ui_version": selectors.UI_VERSION,
        "exit_code": exit_code,
        "label": label,
        "message": message,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "last_url": last_url,
        "sources_count": sources_count,
    }
    if extra:
        meta.update(extra)
    path = out_dir / "meta.json"
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def setup_logger(out_dir: Path, *, redact_sink: bool = True) -> logging.Logger:
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"deep_research.{out_dir.name}")
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = logging.FileHandler(out_dir / "run.log", encoding="utf-8")
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return redact(msg)


def run_query(
    inputs: RunInputs,
    *,
    browser_factory: Callable[..., Any] | None = None,
) -> RunResult:
    """Drive the Gemini UI to completion.

    The browser is fully encapsulated: a successful run produces
    `report.md`, `meta.json`, `screenshot.png`, `run.log` in `out_dir`.

    Pass `browser_factory` to inject a fake Playwright session in tests.
    Default uses the real Playwright sync API.
    """
    inputs.out_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(inputs.out_dir)
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t0 = time.monotonic()
    last_url: str | None = None
    label = "ok"
    extra: dict[str, Any] = {}

    try:
        assert_inputs(inputs)
        with acquire_lock():
            ensure_profile_dir()
            factory = browser_factory or _default_playwright_factory
            ctx = factory(headed=inputs.headed)
            try:
                page = ctx.new_page()
                logger.info("navigating to %s", GEMINI_URL)
                page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60_000)
                last_url = page.url

                err = map_url_to_error(last_url)
                if err is not None:
                    raise DeepResearchError(err, "Gemini redirected to sign-in", last_url=last_url)

                _select_deep_research(page, logger, nav_model=inputs.nav_model)
                _submit_query(page, build_prompt(inputs.query), logger)
                _start_research_plan(page, logger)
                body_markdown, sources_text, title = _wait_for_completion_and_export(
                    page, logger, inputs.max_wait_seconds
                )
                last_url = page.url

                screenshot_path = inputs.out_dir / "screenshot.png"
                try:
                    page.screenshot(path=str(screenshot_path), full_page=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("screenshot failed: %s", exc)
                    screenshot_path = None

                sources = parse_sources(sources_text)
                finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                duration = time.monotonic() - t0

                report_md = render_report(
                    query=inputs.query,
                    job_id=inputs.job_id,
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_seconds=duration,
                    body_markdown=body_markdown,
                    sources=sources,
                    exit_code=EXIT_OK,
                    title=title,
                )
                report_path = inputs.out_dir / "report.md"
                report_path.write_text(report_md, encoding="utf-8")
                meta_path = write_meta(
                    inputs.out_dir,
                    job_id=inputs.job_id,
                    query=inputs.query,
                    exit_code=EXIT_OK,
                    label="ok",
                    message="ok",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_seconds=duration,
                    last_url=last_url,
                    sources_count=len(sources),
                    extra=extra,
                )
                return RunResult(
                    exit_code=EXIT_OK,
                    message="ok",
                    report_path=report_path,
                    meta_path=meta_path,
                    screenshot_path=screenshot_path,
                    duration_seconds=duration,
                    sources_count=len(sources),
                    last_url=last_url,
                    label="ok",
                    extra=extra,
                )
            finally:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass
    except DeepResearchError as exc:
        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        duration = time.monotonic() - t0
        logger.error("deep-research failed [%s]: %s", exc.label, exc.message)
        meta_path = write_meta(
            inputs.out_dir,
            job_id=inputs.job_id,
            query=inputs.query,
            exit_code=exc.code,
            label=exc.label,
            message=exc.message,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            last_url=last_url or exc.last_url,
            sources_count=0,
            extra=extra,
        )
        return RunResult(
            exit_code=exc.code,
            message=exc.message,
            meta_path=meta_path,
            duration_seconds=duration,
            sources_count=0,
            last_url=last_url or exc.last_url,
            label=exc.label,
            extra=extra,
        )
    except Exception as exc:  # noqa: BLE001
        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        duration = time.monotonic() - t0
        logger.exception("deep-research crashed: %s", exc)
        meta_path = write_meta(
            inputs.out_dir,
            job_id=inputs.job_id,
            query=inputs.query,
            exit_code=EXIT_BROWSER_CRASH,
            label="browser_crash",
            message=str(exc),
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            last_url=last_url,
            sources_count=0,
            extra=extra,
        )
        return RunResult(
            exit_code=EXIT_BROWSER_CRASH,
            message=str(exc),
            meta_path=meta_path,
            duration_seconds=duration,
            sources_count=0,
            last_url=last_url,
            label="browser_crash",
            extra=extra,
        )


# --- internal browser steps -------------------------------------------------


def _find_system_chrome() -> str | None:
    """Return path to system Chrome/Chromium if Playwright's bundle is absent."""
    import shutil

    env_override = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if env_override:
        return env_override
    for candidate in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _default_playwright_factory(*, headed: bool) -> Any:
    """Open a Playwright persistent context against the shared profile.

    Imported lazily so test suites without playwright still load the module.
    Falls back to system Chrome when Playwright's bundled Chromium is absent
    (e.g. Ubuntu 26+ where the bundle isn't supported yet).
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised in install path only
        raise DeepResearchError(
            EXIT_BROWSER_CRASH,
            "Playwright is not installed. Run install.sh or "
            "`pip install playwright && playwright install chromium`.",
        ) from exc

    import subprocess as _sp

    profile = ensure_profile_dir()
    pw = sync_playwright().start()

    # Detect whether Playwright's own Chromium bundle exists.
    try:
        _sp.run(
            [pw.chromium.executable_path, "--version"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        system_exe: str | None = None
    except Exception:
        system_exe = _find_system_chrome()

    launch_kwargs: dict = {
        "headless": not headed,
        "args": ["--disable-blink-features=AutomationControlled"],
        "viewport": {"width": 1280, "height": 900},
    }
    if system_exe:
        launch_kwargs["executable_path"] = system_exe

    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            **launch_kwargs,
        )
    except Exception:
        pw.stop()
        raise

    class _Wrapper:
        def __init__(self, _pw: Any, _ctx: Any) -> None:
            self._pw = _pw
            self._ctx = _ctx

        def new_page(self) -> Any:
            pages = self._ctx.pages
            return pages[0] if pages else self._ctx.new_page()

        def close(self) -> None:
            try:
                self._ctx.close()
            finally:
                self._pw.stop()

    return _Wrapper(pw, context)


def _try_selectors(page: Any, sel: selectors.Selector, *, timeout_ms: int = 5_000) -> Any | None:
    """Return the first locator that matches; never raise."""
    for css in sel.all():
        try:
            loc = page.locator(css).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _agent_step(page: Any, sel: selectors.Selector, *, budget_seconds: int = 30) -> bool:
    """Hand the failing step to a browser-use Agent.

    Best-effort: returns True if the agent claims success, False on any
    failure or if browser-use is unavailable. The caller decides whether
    to escalate to `EXIT_SELECTORS_FAILED`.
    """
    try:
        from browser_use import Agent  # type: ignore
    except ImportError:
        return False

    nav_model = os.environ.get(NAV_MODEL_ENV, DEFAULT_NAV_MODEL)
    try:
        agent = Agent(task=sel.goal, llm=nav_model, page=page)
        agent.run(max_steps=10, max_seconds=budget_seconds)
        return True
    except Exception:  # noqa: BLE001
        return False


def _select_deep_research(page: Any, logger: logging.Logger, *, nav_model: str | None) -> None:
    btn = _try_selectors(page, selectors.MODEL_SWITCH)
    if btn is None:
        if not _agent_step(page, selectors.MODEL_SWITCH):
            raise DeepResearchError(
                EXIT_SELECTORS_FAILED,
                "Could not open the Gemini model selector.",
                last_url=getattr(page, "url", None),
            )
    else:
        try:
            btn.click(timeout=5_000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("model switch click failed: %s", exc)
            if not _agent_step(page, selectors.MODEL_SWITCH):
                raise DeepResearchError(
                    EXIT_SELECTORS_FAILED,
                    f"Model switch click failed: {exc}",
                    last_url=getattr(page, "url", None),
                ) from exc

    option = _try_selectors(page, selectors.DEEP_RESEARCH_OPTION, timeout_ms=8_000)
    if option is None:
        if not _agent_step(page, selectors.DEEP_RESEARCH_OPTION):
            raise DeepResearchError(
                EXIT_DEEP_RESEARCH_UNAVAILABLE,
                "Deep Research option not present in the model menu (no subscription or unsupported region).",
                last_url=getattr(page, "url", None),
            )
        return
    try:
        option.click(timeout=5_000)
    except Exception as exc:  # noqa: BLE001
        raise DeepResearchError(
            EXIT_DEEP_RESEARCH_UNAVAILABLE,
            f"Deep Research option click failed: {exc}",
            last_url=getattr(page, "url", None),
        ) from exc


def _submit_query(page: Any, prompt: str, logger: logging.Logger) -> None:
    box = _try_selectors(page, selectors.CHAT_INPUT, timeout_ms=10_000)
    if box is None:
        if not _agent_step(page, selectors.CHAT_INPUT):
            raise DeepResearchError(
                EXIT_SELECTORS_FAILED,
                "Could not focus the Gemini prompt input.",
                last_url=getattr(page, "url", None),
            )
        return
    try:
        box.fill(prompt, timeout=10_000)
    except Exception:
        try:
            box.click()
            page.keyboard.type(prompt, delay=10)
        except Exception as exc:  # noqa: BLE001
            raise DeepResearchError(
                EXIT_SELECTORS_FAILED,
                f"Could not type the prompt: {exc}",
                last_url=getattr(page, "url", None),
            ) from exc
    try:
        page.keyboard.press("Enter")
    except Exception as exc:  # noqa: BLE001
        raise DeepResearchError(
            EXIT_SELECTORS_FAILED,
            f"Could not submit prompt: {exc}",
            last_url=getattr(page, "url", None),
        ) from exc


def _start_research_plan(page: Any, logger: logging.Logger) -> None:
    btn = _try_selectors(page, selectors.PLAN_CARD, timeout_ms=60_000)
    if btn is None:
        if not _agent_step(page, selectors.PLAN_CARD, budget_seconds=45):
            raise DeepResearchError(
                EXIT_SELECTORS_FAILED,
                "Plan card never offered a Start research button.",
                last_url=getattr(page, "url", None),
            )
        return
    try:
        btn.click(timeout=5_000)
    except Exception as exc:  # noqa: BLE001
        raise DeepResearchError(
            EXIT_SELECTORS_FAILED,
            f"Could not start research plan: {exc}",
            last_url=getattr(page, "url", None),
        ) from exc


def _wait_for_completion_and_export(
    page: Any,
    logger: logging.Logger,
    max_wait_seconds: int,
) -> tuple[str, str, str | None]:
    deadline = time.monotonic() + max_wait_seconds
    poll_interval = 5
    while time.monotonic() < deadline:
        try:
            text = (page.content() or "")[:200_000]
        except Exception as exc:  # noqa: BLE001
            logger.warning("content() failed during poll: %s", exc)
            text = ""
        err = map_text_to_error(text) or map_url_to_error(getattr(page, "url", "") or "")
        if err is not None:
            raise DeepResearchError(err, f"page indicates failure: code {err}", last_url=getattr(page, "url", None))
        if any(frag.lower() in text.lower() for frag in selectors.COMPLETION_TEXT_FRAGMENTS):
            break
        time.sleep(poll_interval)
    else:
        raise DeepResearchError(
            EXIT_SELECTORS_FAILED,
            f"Research did not complete within {max_wait_seconds}s.",
            last_url=getattr(page, "url", None),
        )

    body_markdown = _export_markdown(page, logger)
    sources_text = _extract_sources_text(page, logger)
    title = _extract_title(page)
    return body_markdown, sources_text, title


def _export_markdown(page: Any, logger: logging.Logger) -> str:
    btn = _try_selectors(page, selectors.EXPORT_MD, timeout_ms=10_000)
    if btn is not None:
        try:
            btn.click(timeout=5_000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("export click failed: %s", exc)
    try:
        body = page.locator("main").first.inner_text(timeout=15_000)
    except Exception as exc:  # noqa: BLE001
        raise DeepResearchError(
            EXIT_SELECTORS_FAILED,
            f"Could not read research body: {exc}",
            last_url=getattr(page, "url", None),
        ) from exc
    return body or ""


def _extract_sources_text(page: Any, logger: logging.Logger) -> str:
    panel = _try_selectors(page, selectors.SOURCES_LIST, timeout_ms=8_000)
    if panel is None:
        return ""
    try:
        return panel.inner_text(timeout=5_000) or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("sources extract failed: %s", exc)
        return ""


def _extract_title(page: Any) -> str | None:
    try:
        h1 = page.locator("main h1").first
        h1.wait_for(state="visible", timeout=2_000)
        text = (h1.inner_text(timeout=2_000) or "").strip()
        return text or None
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "RunInputs",
    "RunResult",
    "GEMINI_URL",
    "NAV_MODEL_ENV",
    "DEFAULT_NAV_MODEL",
    "DISABLED_ENV",
    "assert_inputs",
    "build_prompt",
    "redact",
    "parse_sources",
    "map_url_to_error",
    "map_text_to_error",
    "render_report",
    "write_meta",
    "setup_logger",
    "run_query",
]
