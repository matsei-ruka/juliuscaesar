"""Heartbeat task runner.

Reads an instance's tasks.yaml, runs pre_fetch if configured, hash-checks
for delta (optional), auto-prepends the instance's L1 memory as context,
dispatches to a framework adapter, and delivers via Telegram.

Framework code lives alongside this file (adapters/, lib/send_telegram.sh).
Instance code (tasks.yaml, fetch scripts, state dir) lives under
<instance_dir>/heartbeat/.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from jc_paths import resolve_instance_path

from . import builtins as _builtins


FRAMEWORK_ROOT = Path(__file__).resolve().parent  # lib/heartbeat/
ADAPTERS_DIR = FRAMEWORK_ROOT / "adapters"
SEND_TELEGRAM = FRAMEWORK_ROOT / "lib" / "send_telegram.sh"


def log_line(msg: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}\n")


def load_tasks(tasks_file: Path) -> dict:
    if not tasks_file.exists():
        raise FileNotFoundError(f"tasks.yaml not found at {tasks_file}")
    with tasks_file.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve(task: dict, defaults: dict, key: str, fallback=None):
    if key in task and task[key] not in (None, ""):
        return task[key]
    if key in defaults and defaults[key] not in (None, ""):
        return defaults[key]
    return fallback


def render_prompt_template(template: str, substitutions: dict) -> str:
    out = template
    for k, v in substitutions.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def load_l1_context(instance_dir: Path) -> str:
    l1 = instance_dir / "memory" / "L1"
    if not l1.exists():
        return ""
    parts = []
    for p in sorted(l1.glob("*.md")):
        parts.append(f"\n--- {p.name} ---\n{p.read_text(encoding='utf-8')}")
    return "".join(parts)


def load_context_files(instance_dir: Path, paths: list[str], *, allowlist=()) -> str:
    parts = []
    for rel in paths or []:
        p = resolve_instance_path(instance_dir, rel, allowlist=allowlist)
        if not p.exists():
            parts.append(f"\n--- {rel} (MISSING) ---\n")
            continue
        parts.append(f"\n--- {rel} ---\n{p.read_text(encoding='utf-8')}")
    return "".join(parts)


def _terminate_process_group(proc: subprocess.Popen, *, grace_seconds: float = 5.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def run_pre_fetch(
    instance_dir: Path,
    script_rel: str,
    bundle_path: Path,
    log_path: Path,
    *,
    timeout_seconds: int | None = None,
) -> None:
    script = resolve_instance_path(instance_dir, Path("heartbeat") / script_rel)
    if not script.exists():
        raise FileNotFoundError(f"pre_fetch script not found: {script}")
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with bundle_path.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            ["bash", str(script)],
            stdout=out,
            stderr=subprocess.PIPE,
            cwd=str(instance_dir / "heartbeat"),
            start_new_session=True,
        )
        try:
            _, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            log_line(f"pre_fetch TIMEOUT timeout={timeout_seconds}s script={script_rel}", log_path)
            raise TimeoutError(f"pre_fetch timeout after {timeout_seconds}s")
    if proc.returncode != 0:
        log_line(f"pre_fetch FAILED rc={proc.returncode} stderr={stderr.decode()[:500]}", log_path)
        raise RuntimeError(f"pre_fetch failed: {proc.returncode}")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _claude_proj_dir(instance_dir: Path) -> Path:
    slug = str(instance_dir).replace("/", "-").replace("_", "-")
    return Path.home() / ".claude" / "projects" / slug


def snapshot_jsonl(proj_dir: Path) -> dict[str, float]:
    """Snapshot all .jsonl stems + mtimes in proj_dir. Used for session diff."""
    if not proj_dir.is_dir():
        return {}
    result = {}
    for p in proj_dir.glob("*.jsonl"):
        try:
            result[p.stem] = p.stat().st_mtime
        except OSError:
            pass
    return result


def capture_session_id(
    instance_dir: Path,
    tool: str,
    prior_session: str | None,
    pre_snapshot: dict[str, float],
) -> str | None:
    """Capture session ID after adapter run via snapshot diff (no timing race).

    If we resumed a known session, confirm it was written and return it.
    Otherwise, find the one new JSONL that wasn't in pre_snapshot.
    """
    if tool != "claude":
        return None
    proj_dir = _claude_proj_dir(instance_dir)
    if not proj_dir.is_dir():
        return None

    if prior_session:
        jsonl = proj_dir / f"{prior_session}.jsonl"
        try:
            if jsonl.stat().st_mtime > pre_snapshot.get(prior_session, 0):
                return prior_session
        except OSError:
            pass
        # Session file not written (expired or missing) — fall through to new-session search.

    for p in proj_dir.glob("*.jsonl"):
        if p.stem not in pre_snapshot:
            return p.stem
    return None


def load_session_id(instance_dir: Path, task_name: str) -> str | None:
    """Load saved session ID from prior run, if exists."""
    session_file = instance_dir / "heartbeat" / "state" / f"{task_name}.session"
    if not session_file.exists():
        return None
    try:
        return session_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def save_session_id(instance_dir: Path, task_name: str, session_id: str) -> None:
    """Save session ID for next run's --resume."""
    session_file = instance_dir / "heartbeat" / "state" / f"{task_name}.session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(session_id, encoding="utf-8")


def call_adapter(
    tool: str,
    model: str | None,
    prompt: str,
    workdir: Path,
    log_path: Path,
    *,
    timeout_seconds: int | None = None,
) -> str:
    adapter = ADAPTERS_DIR / f"{tool}.sh"
    if not adapter.exists():
        raise FileNotFoundError(f"adapter not found: {adapter}")
    if not os.access(adapter, os.X_OK):
        raise PermissionError(f"adapter not executable: {adapter}")
    proc = subprocess.Popen(
        [str(adapter), model or ""],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(workdir),
        start_new_session=True,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(prompt, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        log_line(f"adapter TIMEOUT tool={tool} timeout={timeout_seconds}s", log_path)
        raise TimeoutError(f"adapter {tool} timeout after {timeout_seconds}s")
    if proc.returncode != 0:
        log_line(f"adapter FAILED tool={tool} rc={proc.returncode} stderr={stderr[:500]}", log_path)
        raise RuntimeError(f"adapter {tool} failed: {proc.returncode}\n{stderr[:500]}")
    return stdout


def send_telegram(
    instance_dir: Path,
    body: str,
    log_path: Path,
    chat_id_override: str | None = None,
) -> str | None:
    """Send via the framework's send_telegram.sh.

    By default, token + chat_id are read from the instance's .env. Pass
    `chat_id_override` to send to a specific chat (used for named
    destinations). Returns message_id as string on success, or None.
    """
    env = os.environ.copy()
    env["JC_INSTANCE_DIR"] = str(instance_dir)
    if chat_id_override:
        env["TELEGRAM_CHAT_ID_OVERRIDE"] = str(chat_id_override)
    r = subprocess.run(
        [str(SEND_TELEGRAM)],
        input=body,
        capture_output=True,
        text=True,
        env=env,
    )
    if r.returncode != 0:
        log_line(f"telegram send FAILED rc={r.returncode} stderr={r.stderr[:500]}", log_path)
        return None
    return r.stdout.strip()


def resolve_destinations(task: dict, defaults: dict, all_destinations: dict) -> list[dict]:
    """Resolve a task's `destination:` field into a list of destination configs.

    Precedence:
      1. task.destination (string or list of names)
      2. defaults.destination (string or list of names)
      3. [] — caller falls back to legacy env-var behavior

    Each returned item has the shape:
      {"name": str, "channel": str, "chat_id": str}

    Raises ValueError if a referenced destination name isn't defined.
    """
    raw = task.get("destination")
    if raw is None:
        raw = defaults.get("destination")
    if raw is None:
        return []
    names = [raw] if isinstance(raw, str) else list(raw)
    resolved = []
    for name in names:
        if name not in all_destinations:
            raise ValueError(f"unknown destination '{name}' (not defined in destinations:)")
        d = all_destinations[name]
        if not isinstance(d, dict) or "chat_id" not in d:
            raise ValueError(f"destination '{name}' must be a mapping with a chat_id")
        resolved.append(
            {
                "name": name,
                "channel": d.get("channel", "telegram"),
                "chat_id": str(d["chat_id"]),
            }
        )
    return resolved


def _run_builtin_task(
    instance_dir: Path,
    *,
    task_name: str,
    builtin_name: str,
    task: dict,
    state: Path,
    ts_tag: str,
    log_path: Path,
    dry_run: bool,
) -> int:
    """Dispatch a ``builtin: <name>`` task to its Python handler."""
    fn = _builtins.get(builtin_name)
    if fn is None:
        log_line(
            f"task {task_name}: unknown builtin {builtin_name!r} "
            f"(known: {', '.join(_builtins.names())})",
            log_path,
        )
        print(f"Unknown builtin: {builtin_name}", file=sys.stderr)
        return 2
    enabled = task.get("enabled", False)
    # Builtins ship disabled — operator opts in by setting `enabled: true`.
    if not enabled and not dry_run:
        log_line(
            f"task {task_name}: builtin {builtin_name} not enabled; set "
            f"`enabled: true` in tasks.yaml to commit changes",
            log_path,
        )
        # Treat the disabled state as an automatic dry-run so cron schedules
        # can be set up before flipping the bit.
        dry_run = True
    log_line(
        f"task {task_name}: builtin={builtin_name} dry_run={dry_run}",
        log_path,
    )
    try:
        summary = fn(instance_dir, dry_run)
    except Exception as exc:  # noqa: BLE001
        log_line(
            f"task {task_name}: builtin {builtin_name} raised {exc!r}",
            log_path,
        )
        return 1
    output_path = state / "outputs" / f"{task_name}-{ts_tag}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log_line(
        f"task {task_name}: builtin {builtin_name} ok output={output_path}",
        log_path,
    )
    if dry_run:
        # Print a compact summary so operators can preview decisions.
        print(json.dumps(summary, indent=2))
    return 0 if summary.get("ok", True) else 1


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("w")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fh.write(f"{os.getpid()}\n")
            self.fh.flush()
            return True
        except BlockingIOError:
            self.fh.close()
            self.fh = None
            return False

    def release(self) -> None:
        if self.fh is not None:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()
                self.fh = None


def run_task(instance_dir: Path, task_name: str, dry_run: bool = False) -> int:
    """Execute one task. Returns process exit code (0 = success or silent skip)."""
    instance_dir = instance_dir.resolve()
    heartbeat_dir = instance_dir / "heartbeat"
    state = heartbeat_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    log_path = state / "run.log"

    ts = dt.datetime.now()
    ts_tag = ts.strftime("%Y%m%dT%H%M%S")

    # Source instance .env (secrets like TELEGRAM_BOT_TOKEN, DASHSCOPE_API_KEY)
    env_file = instance_dir / ".env"
    if env_file.exists():
        load_dotenv(str(env_file))

    # Load tasks
    tasks_file = heartbeat_dir / "tasks.yaml"
    cfg = load_tasks(tasks_file)
    defaults = cfg.get("defaults") or {}
    tasks = cfg.get("tasks") or {}
    destinations = cfg.get("destinations") or {}
    if task_name not in tasks:
        log_line(f"unknown task: {task_name}", log_path)
        print(f"Unknown task: {task_name}", file=sys.stderr)
        return 2
    task = tasks[task_name]

    # Lock
    lock = FileLock(state / f"{task_name}.lock")
    if not lock.acquire():
        log_line(f"task {task_name}: lock held, skipping", log_path)
        return 0

    try:
        # Built-in tasks short-circuit the LLM dispatch path. Used for things
        # like hot_tidy that need to run pure-Python over instance state.
        builtin_name = task.get("builtin")
        if builtin_name:
            return _run_builtin_task(
                instance_dir,
                task_name=task_name,
                builtin_name=str(builtin_name),
                task=task,
                state=state,
                ts_tag=ts_tag,
                log_path=log_path,
                dry_run=dry_run,
            )

        tool = resolve(task, defaults, "tool", "claude")
        model = resolve(task, defaults, "model", None)
        allowlist = list(defaults.get("path_allowlist") or []) + list(task.get("path_allowlist") or [])
        folder = resolve_instance_path(
            instance_dir,
            resolve(task, defaults, "folder", str(instance_dir)),
            allowlist=allowlist,
        )
        pre_fetch = resolve(task, defaults, "pre_fetch", None)
        timeout_seconds = resolve(task, defaults, "timeout_seconds", None)
        pre_fetch_timeout_seconds = resolve(task, defaults, "pre_fetch_timeout_seconds", None)
        timeout_seconds = int(timeout_seconds) if timeout_seconds is not None else None
        pre_fetch_timeout_seconds = (
            int(pre_fetch_timeout_seconds)
            if pre_fetch_timeout_seconds is not None
            else timeout_seconds
        )
        prompt_tpl = task.get("prompt") or ""
        only_if_delta = bool(resolve(task, defaults, "only_if_delta", False))
        context_files = task.get("context_files") or []

        bundle_path = None
        if pre_fetch:
            bundle_path = state / "bundles" / f"{task_name}-{ts_tag}.md"
            run_pre_fetch(
                instance_dir,
                pre_fetch,
                bundle_path,
                log_path,
                timeout_seconds=pre_fetch_timeout_seconds,
            )

            if only_if_delta:
                new_hash = sha256_file(bundle_path)
                last_hash_file = state / f"{task_name}.last_hash"
                if last_hash_file.exists() and last_hash_file.read_text().strip() == new_hash:
                    log_line(f"task {task_name}: no delta, skipping", log_path)
                    return 0
                last_hash_file.write_text(new_hash)

        l1_ctx = load_l1_context(instance_dir)
        extra_ctx = load_context_files(instance_dir, context_files, allowlist=allowlist)
        bundle_body = bundle_path.read_text(encoding="utf-8") if bundle_path else ""

        subs = {
            "bundle_path": str(bundle_path) if bundle_path else "",
            "date": ts.strftime("%Y-%m-%d"),
            "time": ts.strftime("%H:%M"),
            "timezone": os.environ.get("TZ", "UTC"),
        }
        task_prompt = render_prompt_template(prompt_tpl, subs)

        parts = ["=== L1 memory (always loaded) ===", l1_ctx]
        if extra_ctx:
            parts += ["", "=== Additional context ===", extra_ctx]
        parts += ["", f"=== Task: {task_name} ===", task_prompt]
        if bundle_body:
            parts += ["", "=== Bundle ===", bundle_body]
        final_prompt = "\n".join(parts)

        prompt_path = state / "prompts" / f"{task_name}-{ts_tag}.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(final_prompt, encoding="utf-8")

        log_line(f"task {task_name}: tool={tool} model={model or '(default)'} folder={folder}", log_path)

        # Load prior session ID if available (for --resume).
        prior_session = load_session_id(instance_dir, task_name)
        if prior_session:
            os.environ["JC_RESUME_SESSION"] = prior_session
            log_line(f"task {task_name}: resuming session {prior_session}", log_path)

        # Snapshot before adapter so we can find the new/resumed session by diff, not mtime.
        pre_snapshot = snapshot_jsonl(_claude_proj_dir(instance_dir)) if tool == "claude" else {}

        output = call_adapter(
            tool,
            model,
            final_prompt,
            folder,
            log_path,
            timeout_seconds=timeout_seconds,
        )

        # Capture session ID for next run.
        new_session = capture_session_id(instance_dir, tool, prior_session, pre_snapshot)
        if new_session:
            save_session_id(instance_dir, task_name, new_session)
            log_line(f"task {task_name}: captured session {new_session}", log_path)
            if "JC_RESUME_SESSION" in os.environ:
                del os.environ["JC_RESUME_SESSION"]

        output_path = state / "outputs" / f"{task_name}-{ts_tag}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")

        stripped = output.strip()
        if not stripped or stripped == "SILENT":
            log_line(f"task {task_name}: silent/empty output, no Telegram send", log_path)
            return 0

        if dry_run:
            print(output)
            return 0

        tag = f"[{task_name} · {ts.strftime('%H:%M')}]"
        body = f"{tag}\n\n{output.strip()}"

        # Resolve destinations. If none configured, fall back to legacy
        # env-var behavior (send_telegram uses TELEGRAM_CHAT_ID from .env).
        try:
            dest_list = resolve_destinations(task, defaults, destinations)
        except ValueError as e:
            log_line(f"task {task_name}: destination resolution failed — {e}", log_path)
            return 1

        sent_log = state / "sent.log"

        if not dest_list:
            # Legacy path: single send to TELEGRAM_CHAT_ID from .env
            msg_id = send_telegram(instance_dir, body, log_path)
            if msg_id:
                with sent_log.open("a", encoding="utf-8") as f:
                    f.write(
                        f"message_id={msg_id}  task={task_name}  "
                        f"ts={ts.isoformat(timespec='seconds')}  "
                        f"destination=-  "
                        f"prompt={prompt_path}  output={output_path}  "
                        f"bundle={bundle_path if bundle_path else '-'}\n"
                    )
                log_line(f"task {task_name}: sent message_id={msg_id}", log_path)
        else:
            # Named destinations: one send per destination; log each.
            for d in dest_list:
                if d["channel"] != "telegram":
                    log_line(
                        f"task {task_name}: destination '{d['name']}' has unsupported "
                        f"channel '{d['channel']}' — skipped (telegram only in 0.1.x)",
                        log_path,
                    )
                    continue
                msg_id = send_telegram(instance_dir, body, log_path, chat_id_override=d["chat_id"])
                if msg_id:
                    with sent_log.open("a", encoding="utf-8") as f:
                        f.write(
                            f"message_id={msg_id}  task={task_name}  "
                            f"ts={ts.isoformat(timespec='seconds')}  "
                            f"destination={d['name']}  "
                            f"prompt={prompt_path}  output={output_path}  "
                            f"bundle={bundle_path if bundle_path else '-'}\n"
                        )
                    log_line(
                        f"task {task_name}: sent message_id={msg_id} to destination={d['name']}",
                        log_path,
                    )

        return 0
    finally:
        lock.release()
