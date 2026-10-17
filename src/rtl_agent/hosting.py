"""Explicit hosted-mode settings, access checks and a durable shared run quota."""

import hashlib
import hmac
import os
import re
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    password: str = field(repr=False)
    daily_runs: int = 10
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
        if (
            not 24 <= len(self.password) <= 256
            or not self.password.isascii()
            or any(not 33 <= ord(c) <= 126 for c in self.password)
        ):
            raise ValueError(
                "Set RTL_AGENT_LIVE_PASSWORD in backend secrets: 24–256 non-space ASCII characters."
            )
        if not 1 <= self.daily_runs <= 100:
            raise ValueError("RTL_AGENT_DAILY_RUN_LIMIT must be between 1 and 100.")
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
            password=os.environ.get("RTL_AGENT_LIVE_PASSWORD", ""),
            daily_runs=integer("RTL_AGENT_DAILY_RUN_LIMIT", 10),
            max_attempts=integer("RTL_AGENT_HOSTED_MAX_ATTEMPTS", 3),
            run_timeout=integer("RTL_AGENT_HOSTED_RUN_TIMEOUT", 300),
        )


class HostedAccess:
    def __init__(self, settings: HostedSettings):
        self.settings = settings
        self.password_hash = hashlib.sha256(settings.password.encode()).digest()
        self.failures: deque[float] = deque()
        self.lock = threading.Lock()

    def authenticate(self, authorization: str | None, *, required: bool) -> bool:
        if not authorization and not required:
            return False
        prefix, _, value = (authorization or "").partition(" ")
        supplied = hashlib.sha256(value.encode()).digest()
        if prefix.lower() == "bearer" and hmac.compare_digest(supplied, self.password_hash):
            return True
        # One global bucket also works behind proxies without trusting forwarded IP headers.
        with self.lock:
            now = time.monotonic()
            while self.failures and self.failures[0] < now - 60:
                self.failures.popleft()
            if len(self.failures) >= 20:
                raise HTTPException(
                    429,
                    "Too many access attempts. Try again in a minute.",
                    headers={"Retry-After": "60"},
                )
            self.failures.append(now)
        raise HTTPException(401, "Live access is locked. Enter the access password.")


class DailyQuota:
    """Count admitted runs, including failures and stops; restarts keep the count."""

    def __init__(self, root: Path, limit: int):
        self.path = root / ".hosted-usage.sqlite3"
        self.limit = limit
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, runs INTEGER NOT NULL)"
            )

    def reserve(self) -> None:
        day = datetime.now(UTC).date().isoformat()
        try:
            with sqlite3.connect(self.path, timeout=5) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT runs FROM usage WHERE day = ?", (day,)).fetchone()
                used = row[0] if row else 0
                if used >= self.limit:
                    raise HTTPException(
                        429,
                        "The shared daily Live run limit is reached. Replay is still available.",
                    )
                db.execute(
                    "INSERT INTO usage(day, runs) VALUES (?, 1) "
                    "ON CONFLICT(day) DO UPDATE SET runs = runs + 1",
                    (day,),
                )
                db.execute("DELETE FROM usage WHERE day < date(?, '-7 days')", (day,))
        except sqlite3.Error:
            raise HTTPException(503, "Live usage limits are temporarily unavailable.") from None
