"""Durable state between commands.

Holds what must survive a restart: the last command and its outcome, the
duplicate-suppression record, the daily command counter, pending live
confirmations, and — most importantly — the "brokers disagree" flag that
blocks further trading until a human acknowledges it.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import DuplicateCommand, SafetyLimitExceeded, UnreconciledState


@dataclass
class PendingConfirmation:
    token: str
    command: str
    created_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass
class StateData:
    last_command: str | None = None
    last_command_at: float = 0.0
    last_command_id: str | None = None
    last_result: dict[str, Any] | None = None
    daily_counts: dict[str, int] = field(default_factory=dict)
    seen_request_ids: dict[str, str] = field(default_factory=dict)
    mismatch: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


class StateStore:
    """JSON-backed, thread-safe, written atomically."""

    MAX_HISTORY = 100

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._pending: dict[str, PendingConfirmation] = {}
        self.data = self._load()

    def _load(self) -> StateData:
        if not self.path.exists():
            return StateData()
        try:
            raw = json.loads(self.path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            return StateData()
        return StateData(
            last_command=raw.get("last_command"),
            last_command_at=float(raw.get("last_command_at", 0) or 0),
            last_command_id=raw.get("last_command_id"),
            last_result=raw.get("last_result"),
            daily_counts=dict(raw.get("daily_counts", {})),
            seen_request_ids=dict(raw.get("seen_request_ids", {})),
            mismatch=raw.get("mismatch"),
            history=list(raw.get("history", [])),
        )

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            payload = {
                "last_command": self.data.last_command,
                "last_command_at": self.data.last_command_at,
                "last_command_id": self.data.last_command_id,
                "last_result": self.data.last_result,
                "daily_counts": self.data.daily_counts,
                "seen_request_ids": self.data.seen_request_ids,
                "mismatch": self.data.mismatch,
                "history": self.data.history[-self.MAX_HISTORY:],
            }
            tmp.write_text(json.dumps(payload, indent=2, default=str))
            os.replace(tmp, self.path)

    # --- duplicate suppression -------------------------------------------

    def check_duplicate(
        self,
        command: str,
        window_seconds: float,
        request_id: str | None = None,
        force: bool = False,
    ) -> None:
        """Raise :class:`DuplicateCommand` for an accidental repeat.

        Two independent guards:
          * an explicit ``request_id`` that has already been executed, which
            makes a retried HTTP call idempotent; and
          * the same command re-issued inside the time window, which catches
            a double-tap on the dashboard or a Shortcut fired twice.
        """
        with self._lock:
            if request_id and request_id in self.data.seen_request_ids:
                raise DuplicateCommand(
                    f"request_id {request_id!r} was already executed as command "
                    f"{self.data.seen_request_ids[request_id]}. Nothing was "
                    "re-submitted."
                )
            if force or window_seconds <= 0:
                return
            if self.data.last_command != command:
                return
            elapsed = time.time() - self.data.last_command_at
            if elapsed < window_seconds:
                raise DuplicateCommand(
                    f"{command} was already issued {elapsed:.0f}s ago "
                    f"(suppression window {window_seconds:.0f}s). Re-send with "
                    "force=true if you genuinely want to run it again."
                )

    def check_daily_limit(self, max_commands: int) -> None:
        if max_commands <= 0:
            return
        with self._lock:
            key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self.data.daily_counts.get(key, 0) >= max_commands:
                raise SafetyLimitExceeded(
                    f"MAX_DAILY_COMMANDS={max_commands} reached for {key}. "
                    "This is a runaway-automation guard; raise it "
                    "deliberately if the limit is genuinely too low."
                )

    def increment_daily(self) -> None:
        with self._lock:
            key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.data.daily_counts[key] = self.data.daily_counts.get(key, 0) + 1
            # Keep the counter file from growing without bound.
            for old in sorted(self.data.daily_counts)[:-30]:
                self.data.daily_counts.pop(old, None)

    # --- mismatch ---------------------------------------------------------

    def check_mismatch_clear(self, required: bool) -> None:
        if not required:
            return
        with self._lock:
            if self.data.mismatch:
                raise UnreconciledState(
                    "A previous command left the brokers in disagreement "
                    f"({self.data.mismatch.get('summary', 'unknown')}). "
                    "Review both accounts, then acknowledge via "
                    "POST /api/acknowledge (or `cli acknowledge`) before "
                    "trading again."
                )

    def set_mismatch(self, summary: str, detail: dict[str, Any]) -> None:
        with self._lock:
            self.data.mismatch = {
                "summary": summary,
                "detail": detail,
                "at": time.time(),
            }
            self.save()

    def clear_mismatch(self) -> dict[str, Any] | None:
        with self._lock:
            previous = self.data.mismatch
            self.data.mismatch = None
            self.save()
            return previous

    # --- confirmations ----------------------------------------------------

    def issue_confirmation(self, command: str, ttl: float) -> PendingConfirmation:
        with self._lock:
            self._expire_confirmations()
            pending = PendingConfirmation(
                token=secrets.token_urlsafe(16),
                command=command,
                created_at=time.time(),
                expires_at=time.time() + ttl,
            )
            self._pending[pending.token] = pending
            return pending

    def consume_confirmation(self, token: str, command: str) -> bool:
        """One-shot: a token is valid once, for the command it was issued for."""
        with self._lock:
            self._expire_confirmations()
            pending = self._pending.pop(token, None)
            if pending is None:
                return False
            if pending.command != command:
                return False
            return time.time() <= pending.expires_at

    def _expire_confirmations(self) -> None:
        now = time.time()
        for token, pending in list(self._pending.items()):
            if pending.expires_at < now:
                self._pending.pop(token, None)

    # --- results ----------------------------------------------------------

    def record_command(
        self,
        command: str,
        command_id: str,
        request_id: str | None = None,
    ) -> None:
        with self._lock:
            self.data.last_command = command
            self.data.last_command_at = time.time()
            self.data.last_command_id = command_id
            if request_id:
                self.data.seen_request_ids[request_id] = command_id
                if len(self.data.seen_request_ids) > 500:
                    for key in list(self.data.seen_request_ids)[:-500]:
                        self.data.seen_request_ids.pop(key, None)
            self.increment_daily()
            self.save()

    def record_result(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.data.last_result = result
            self.data.history.append(result)
            self.data.history = self.data.history[-self.MAX_HISTORY:]
            self.save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_command": self.data.last_command,
                "last_command_at": self.data.last_command_at,
                "last_command_id": self.data.last_command_id,
                "last_result": self.data.last_result,
                "mismatch": self.data.mismatch,
                "commands_today": self.data.daily_counts.get(
                    datetime.now(timezone.utc).strftime("%Y-%m-%d"), 0
                ),
                "pending_confirmations": [
                    p.to_dict() for p in self._pending.values()
                ],
            }
