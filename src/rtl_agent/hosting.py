"""Hosted settings, anonymous run ownership and a shared daily Live allowance."""

import hashlib
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException


def https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "*" in value
        or parsed.port not in {None, 443}
        or not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname)
    ):
        raise ValueError("Allowed origins must be exact HTTPS website origins, without paths.")
    return f"https://{parsed.hostname.lower()}"


@dataclass(frozen=True)
class HostedSettings:
    backend_host: str
    frontend_origins: tuple[str, ...]
    max_attempts: int = 3
    run_timeout: int = 300

    def __post_init__(self):
        if (
            not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", self.backend_host)
            or ".." in self.backend_host
        ):
            raise ValueError("RTL_AGENT_PUBLIC_HOST must be the backend hostname, without a URL.")
        if not self.frontend_origins:
            raise ValueError("Set RTL_AGENT_ALLOWED_ORIGINS to your exact Vercel HTTPS origin.")
        normalized = tuple(dict.fromkeys(https_origin(origin) for origin in self.frontend_origins))
        object.__setattr__(self, "frontend_origins", normalized)
        object.__setattr__(self, "backend_host", self.backend_host.lower())
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("RTL_AGENT_HOSTED_MAX_ATTEMPTS must be between 1 and 5.")
        if not 10 <= self.run_timeout <= 600:
            raise ValueError("RTL_AGENT_HOSTED_RUN_TIMEOUT must be between 10 and 600 seconds.")

    @property
    def origins(self) -> tuple[str, ...]:
        return (*self.frontend_origins, f"https://{self.backend_host}")

    @classmethod
    def from_env(cls):
        def integer(name, default):
            try:
                return int(os.environ.get(name, str(default)))
            except ValueError:
                raise ValueError(f"{name} must be an integer.") from None

        return cls(
            backend_host=os.environ.get("RTL_AGENT_PUBLIC_HOST", ""),
            frontend_origins=tuple(
                value.strip()
                for value in os.environ.get("RTL_AGENT_ALLOWED_ORIGINS", "").split(",")
                if value.strip()
            ),
            max_attempts=integer("RTL_AGENT_HOSTED_MAX_ATTEMPTS", 3),
            run_timeout=integer("RTL_AGENT_HOSTED_RUN_TIMEOUT", 300),
        )


DAILY_RUN_LIMIT = 20
SESSION_SECONDS = 24 * 60 * 60
SINGAPORE = timezone(timedelta(hours=8), "Asia/Singapore")


def daily_window(now: float) -> tuple[str, float]:
    """Singapore has a fixed UTC+8 offset, with no daylight-saving transitions."""
    today = datetime.fromtimestamp(now, SINGAPORE).date()
    tomorrow = datetime.combine(today + timedelta(days=1), datetime_time(), SINGAPORE)
    return today.isoformat(), tomorrow.timestamp()


class SessionStore:
    """Tokens own runs; one SQLite counter limits all visitors together each day."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / ".hosted-sessions.sqlite3"
        try:
            with sqlite3.connect(self.path) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS sessions ("
                    "id TEXT PRIMARY KEY, runs INTEGER NOT NULL, expires REAL NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS session_runs ("
                    "run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS session_runs_owner ON session_runs(session_id)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS daily_runs ("
                    "day TEXT PRIMARY KEY, runs INTEGER NOT NULL)"
                )
        except sqlite3.Error:
            raise ValueError("Cannot initialize hosted session storage.") from None

    @contextmanager
    def database(self):
        try:
            with sqlite3.connect(self.path, timeout=5) as db:
                yield db
        except sqlite3.Error:
            raise HTTPException(503, "Live session storage is temporarily unavailable.") from None

    def create(self) -> dict:
        token = secrets.token_urlsafe(32)
        session_id = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self.database() as db:
            db.execute(
                "DELETE FROM session_runs WHERE session_id IN "
                "(SELECT id FROM sessions WHERE expires <= ?)",
                (now,),
            )
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            db.execute("INSERT INTO sessions VALUES (?, 0, ?)", (session_id, now + SESSION_SECONDS))
        return {
            "session_token": token,
            "session": self.info(session_id),
            "usage": self.usage(),
        }

    def resolve(self, authorization: str | None, *, required: bool) -> str | None:
        if not authorization and not required:
            return None
        prefix, _, token = (authorization or "").partition(" ")
        if prefix.lower() != "bearer" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise HTTPException(401, "Start a Live session to continue.")
        session_id = hashlib.sha256(token.encode()).hexdigest()
        self.info(session_id)  # Also rejects expired or unknown tokens.
        return session_id

    def info(self, session_id: str) -> dict:
        with self.database() as db:
            row = db.execute(
                "SELECT runs, expires FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None or row[1] <= time.time():
            raise HTTPException(401, "This Live session has ended. Choose Live to start another.")
        return {
            "runs": row[0],
            "expires_at": row[1],
        }

    def usage(self) -> dict:
        day, resets_at = daily_window(time.time())
        with self.database() as db:
            row = db.execute("SELECT runs FROM daily_runs WHERE day = ?", (day,)).fetchone()
        used = row[0] if row else 0
        return {
            "limit": DAILY_RUN_LIMIT,
            "used": used,
            "remaining": max(0, DAILY_RUN_LIMIT - used),
            "day": day,
            "resets_at": resets_at,
            "timezone": "Asia/Singapore",
        }

    def reserve(self, session_id: str) -> None:
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = db.execute(
                "SELECT runs, expires FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None or row[1] <= now:
                raise HTTPException(
                    401, "This Live session has ended. Choose Live to start another."
                )
            day, _ = daily_window(now)
            daily = db.execute("SELECT runs FROM daily_runs WHERE day = ?", (day,)).fetchone()
            if daily and daily[0] >= DAILY_RUN_LIMIT:
                raise HTTPException(
                    429,
                    "Today's 20 Live runs have been used across the site. "
                    "Live resets at midnight Singapore time. Replay is still available.",
                )
            db.execute(
                "INSERT INTO daily_runs VALUES (?, 1) "
                "ON CONFLICT(day) DO UPDATE SET runs = runs + 1",
                (day,),
            )
            db.execute("UPDATE sessions SET runs = runs + 1 WHERE id = ?", (session_id,))

    def bind_run(self, session_id: str, run_id: str) -> None:
        with self.database() as db:
            db.execute("INSERT INTO session_runs VALUES (?, ?)", (run_id, session_id))

    def owned_runs(self, session_id: str) -> set[str]:
        with self.database() as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT run_id FROM session_runs WHERE session_id = ?", (session_id,)
                )
            }

    def require_owner(self, session_id: str, run_id: str) -> None:
        with self.database() as db:
            row = db.execute(
                "SELECT 1 FROM session_runs WHERE session_id = ? AND run_id = ?",
                (session_id, run_id),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Run not found in this session.")
