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
import os
import subprocess
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv


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


def load_context_files(instance_dir: Path, paths: list[str]) -> str:
    parts = []
    for rel in paths or []:
        p = (instance_dir / rel).resolve()
        if not p.exists():
            parts.append(f"\n--- {rel} (MISSING) ---\n")
            continue
        parts.append(f"\n--- {rel} ---\n{p.read_text(encoding='utf-8')}")
    return "".join(parts)


def run_pre_fetch(instance_dir: Path, script_rel: str, bundle_path: Path, log_path: Path) -> None:
    script = (instance_dir / "heartbeat" / script_rel).resolve()
    if not script.exists():
        raise FileNotFoundError(f"pre_fetch script not found: {script}")
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with bundle_path.open("w", encoding="utf-8") as out:
        r = subprocess.run(
            ["bash", str(script)],
            stdout=out,
            stderr=subprocess.PIPE,
            cwd=str(instance_dir / "heartbeat"),
        )
    if r.returncode != 0:
        log_line(f"pre_fetch FAILED rc={r.returncode} stderr={r.stderr.decode()[:500]}", log_path)
        raise RuntimeError(f"pre_fetch failed: {r.returncode}")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def call_adapter(tool: str, model: str | None, prompt: str, workdir: Path, log_path: Path) -> str:
    adapter = ADAPTERS_DIR / f"{tool}.sh"
    if not adapter.exists():
        raise FileNotFoundError(f"adapter not found: {adapter}")
    if not os.access(adapter, os.X_OK):
        raise PermissionError(f"adapter not executable: {adapter}")
    r = subprocess.run(
        [str(adapter), model or ""],
        input=prompt,
        capture_output=True,
        text=True,
        cwd=str(workdir),
    )
    if r.returncode != 0:
        log_line(f"adapter FAILED tool={tool} rc={r.returncode} stderr={r.stderr[:500]}", log_path)
        raise RuntimeError(f"adapter {tool} failed: {r.returncode}\n{r.stderr[:500]}")
    return r.stdout


def send_telegram(instance_dir: Path, body: str, log_path: Path) -> str | None:
    """Send via the framework's send_telegram.sh. Token + chat_id are read
    from the instance's .env by that script. Returns message_id as string
    on success, or None on failure."""
    env = os.environ.copy()
    env["JC_INSTANCE_DIR"] = str(instance_dir)
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
        tool = resolve(task, defaults, "tool", "claude")
        model = resolve(task, defaults, "model", None)
        folder = Path(resolve(task, defaults, "folder", str(instance_dir)))
        pre_fetch = resolve(task, defaults, "pre_fetch", None)
        prompt_tpl = task.get("prompt") or ""
        only_if_delta = bool(resolve(task, defaults, "only_if_delta", False))
        context_files = task.get("context_files") or []

        bundle_path = None
        if pre_fetch:
            bundle_path = state / "bundles" / f"{task_name}-{ts_tag}.md"
            run_pre_fetch(instance_dir, pre_fetch, bundle_path, log_path)

            if only_if_delta:
                new_hash = sha256_file(bundle_path)
                last_hash_file = state / f"{task_name}.last_hash"
                if last_hash_file.exists() and last_hash_file.read_text().strip() == new_hash:
                    log_line(f"task {task_name}: no delta, skipping", log_path)
                    return 0
                last_hash_file.write_text(new_hash)

        l1_ctx = load_l1_context(instance_dir)
        extra_ctx = load_context_files(instance_dir, context_files)
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
        output = call_adapter(tool, model, final_prompt, folder, log_path)

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
        msg_id = send_telegram(instance_dir, body, log_path)

        if msg_id:
            sent_log = state / "sent.log"
            line = (
                f"message_id={msg_id}  task={task_name}  ts={ts.isoformat(timespec='seconds')}  "
                f"prompt={prompt_path}  output={output_path}  "
                f"bundle={bundle_path if bundle_path else '-'}\n"
            )
            with sent_log.open("a", encoding="utf-8") as f:
                f.write(line)
            log_line(f"task {task_name}: sent message_id={msg_id}", log_path)

        return 0
    finally:
        lock.release()
