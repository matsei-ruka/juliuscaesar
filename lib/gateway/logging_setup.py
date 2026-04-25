"""Structured JSON logging for the gateway.

The gateway log is one JSON object per line so `jc gateway logs` can filter
on `--since`, `--class`, `--brain`, `--source`, etc. without parsing free-form
text. Plain-text breadcrumbs are preserved as `msg`.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in (
            "event_id",
            "source",
            "user_id",
            "channel",
            "class",
            "brain",
            "model",
            "reason",
            "latency_ms",
            "result_length",
            "kind",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_logger(
    name: str,
    *,
    log_path: Path,
    max_bytes: int = 50 * 1024 * 1024,
    backups: int = 5,
) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    # Drop any handler we previously installed to make this idempotent.
    for handler in list(logger.handlers):
        if getattr(handler, "_jc_gateway_handler", False):
            logger.removeHandler(handler)
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter())
    handler._jc_gateway_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.propagate = False
    return logger
