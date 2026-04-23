"""JuliusCaesar heartbeat — cron-driven task runner.

The library splits into framework (this dir) and instance content:

Framework (here):
- runner.run_task() — orchestration
- adapters/           — per-tool shell scripts (claude, gemini, ...)
- lib/send_telegram.sh — MCP-independent Telegram delivery

Instance (in the instance dir):
- heartbeat/tasks.yaml — task definitions
- heartbeat/fetch/     — per-task bash pre-fetch scripts (optional)
- heartbeat/state/     — locks, logs, prompts, outputs, bundles, sent.log
"""

from .runner import run_task

__all__ = ["run_task"]
