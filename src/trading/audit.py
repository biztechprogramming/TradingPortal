"""Append-only audit log plus application logging.

Every command, quote, computed quantity, submitted order, modification,
cancellation, fill, error and resulting position lands here as one JSON
object per line. The file is opened in append mode and fsync'd on every
write, so a crash mid-command cannot lose the record of an order that may
already exist at the broker.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings, redact


class AuditEvent(str):
    """Event-type constants (a str subclass so values stay JSON-friendly)."""


# Event types
COMMAND_RECEIVED = "command.received"
COMMAND_REJECTED = "command.rejected"
COMMAND_STARTED = "command.started"
COMMAND_FINISHED = "command.finished"
CONFIRMATION_ISSUED = "command.confirmation_issued"
CONFIRMATION_ACCEPTED = "command.confirmation_accepted"
SNAPSHOT = "broker.snapshot"
RECONCILE = "broker.reconcile"
QUOTE = "market.quote"
QUOTE_REJECTED = "market.quote_rejected"
SIZING = "order.sizing"
ORDER_VALIDATED = "order.validated"
ORDER_SUBMITTED = "order.submitted"
ORDER_REPLACED = "order.replaced"
ORDER_CANCELLED = "order.cancelled"
ORDER_STATUS = "order.status"
ORDER_FILLED = "order.filled"
ORDER_PARTIAL = "order.partial_fill"
ORDER_REJECTED_EVT = "order.rejected"
LEG_COMPLETE = "leg.complete"
ERROR = "error"
WARNING = "warning"
MISMATCH = "safety.broker_mismatch"
MISMATCH_ACK = "safety.mismatch_acknowledged"
LOCK_ACQUIRED = "safety.lock_acquired"
LOCK_CONTENDED = "safety.lock_contended"
DUPLICATE_SUPPRESSED = "safety.duplicate_suppressed"


class AuditLog:
    """Thread-safe JSONL writer with an in-memory tail for the dashboard."""

    def __init__(self, log_dir: Path, tail_size: int = 500) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._tail: deque[dict[str, Any]] = deque(maxlen=tail_size)

    @property
    def path(self) -> Path:
        """One file per UTC day keeps files small and easy to archive."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.log_dir / f"audit-{day}.jsonl"

    def record(
        self,
        event: str,
        *,
        command_id: str | None = None,
        broker: str | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "ts": now,
                "ts_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "event": event,
                "command_id": command_id,
                "broker": broker,
                "data": redact(payload),
            }
            line = json.dumps(record, default=_json_default, separators=(",", ":"))
            path = self.path
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:  # never let logging kill a trade
                logging.getLogger("trading.audit").error(
                    "audit write failed: %s", exc
                )
            self._tail.append(record)
        return record

    def tail(self, limit: int = 100, command_id: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._tail)
        if command_id:
            items = [i for i in items if i.get("command_id") == command_id]
        return items[-limit:]

    def read_day(self, day: str | None = None) -> Iterable[dict]:
        """Stream a day's audit file back, skipping unparsable lines."""
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.log_dir / f"audit-{day}.jsonl"
        if not path.exists():
            return []

        def _iter() -> Iterable[dict]:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

        return _iter()


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "value"):  # Enum
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


class _RedactingFilter(logging.Filter):
    """Belt-and-braces: scrub bearer tokens that slip into log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        lowered = msg.lower()
        if "bearer " in lowered or "refresh_token" in lowered or "access_token" in lowered:
            record.msg = _scrub(msg)
            record.args = ()
        return True


def _scrub(msg: str) -> str:
    import re

    msg = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]+", r"\1***REDACTED***", msg)
    msg = re.sub(
        r'(?i)("?(?:access|refresh|id)_token"?\s*[:=]\s*"?)[A-Za-z0-9._\-+/=]+',
        r"\1***REDACTED***",
        msg,
    )
    return msg


def setup_logging(settings: Settings) -> logging.Logger:
    root = logging.getLogger("trading")
    if root.handlers:  # idempotent across reloads and tests
        return root
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    stream.addFilter(_RedactingFilter())
    root.addHandler(stream)

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fileh = logging.FileHandler(settings.log_dir / "app.log", encoding="utf-8")
    fileh.setFormatter(fmt)
    fileh.addFilter(_RedactingFilter())
    root.addHandler(fileh)
    root.propagate = False
    return root


def new_command_id() -> str:
    return uuid.uuid4().hex[:16]
