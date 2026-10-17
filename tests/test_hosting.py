"""Hosted configuration and quotas never need model or external network access."""

import hashlib
import runpy
import shutil
import sqlite3
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException

from rtl_agent.hosting import HostedSettings, SessionStore


@pytest.mark.parametrize(
    "fields",
    [
        {"backend_host": "https://backend.hf.space"},
        {"backend_host": "bad/host"},
        {"frontend_origins": ()},
        {"frontend_origins": ("https://*.vercel.app",)},
        {"frontend_origins": ("http://site.vercel.app",)},
        {"frontend_origins": ("https://user:password@site.vercel.app",)},
        {"frontend_origins": ("https://site.vercel.app/path",)},
        {"max_attempts": 0},
        {"max_attempts": 6},
        {"run_timeout": 0},
        {"run_timeout": 601},
    ],
)
def test_hosted_settings_reject_incomplete_or_unsafe_settings(fields):
    valid = dict(
        backend_host="backend.hf.space",
        frontend_origins=("https://site.vercel.app",),
    )
    with pytest.raises(ValueError):
        HostedSettings(**(valid | fields))


def test_hosted_env_requires_no_password_or_quota_configuration(monkeypatch):
    monkeypatch.setenv("RTL_AGENT_PUBLIC_HOST", "backend.hf.space")
    monkeypatch.setenv(
        "RTL_AGENT_ALLOWED_ORIGINS", "https://site.vercel.app/, https://site.vercel.app"
    )
    monkeypatch.delenv("RTL_AGENT_LIVE_PASSWORD", raising=False)
    settings = HostedSettings.from_env()
    assert settings.frontend_origins == ("https://site.vercel.app",)
    assert not hasattr(settings, "password")
    monkeypatch.setenv("RTL_AGENT_HOSTED_MAX_ATTEMPTS", "not-an-integer")
    with pytest.raises(ValueError, match="must be an integer"):
        HostedSettings.from_env()


def test_daily_quota_is_atomic_persistent_and_shared_across_sessions(tmp_path):
    store = SessionStore(tmp_path)
    first, second = store.create(), store.create()
    session_id = store.resolve("Bearer " + first["session_token"], required=True)
    second_id = store.resolve("Bearer " + second["session_token"], required=True)
    barrier = threading.Barrier(32)

    def reserve(number):
        barrier.wait()
        try:
            store.reserve([session_id, second_id][number % 2])
            return True
        except HTTPException as exc:
            assert exc.status_code == 429
            return False

    with ThreadPoolExecutor(max_workers=32) as pool:
        assert list(pool.map(reserve, range(32))).count(True) == 20
    assert SessionStore(tmp_path).usage()["remaining"] == 0
    assert store.info(session_id)["runs"] + store.info(second_id)["runs"] == 20
    third = store.create()
    assert third["usage"]["remaining"] == 0
    with pytest.raises(HTTPException) as error:
        store.reserve(store.resolve("Bearer " + third["session_token"], required=True))
    assert error.value.status_code == 429
    store.bind_run(session_id, "owned-run")
    assert SessionStore(tmp_path).owned_runs(session_id) == {"owned-run"}
    assert store.owned_runs(second_id) == set()
    assert session_id == hashlib.sha256(first["session_token"].encode()).hexdigest()
    assert first["session_token"].encode() not in store.path.read_bytes()


def test_shared_quota_resets_at_singapore_midnight_not_utc_midnight(tmp_path, monkeypatch):
    import rtl_agent.hosting as hosting

    def stamp(hour, minute=0, second=0, day=17):
        return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC).timestamp()

    clock = [stamp(15, 59, 59)]  # 23:59:59 in Singapore.
    monkeypatch.setattr(hosting, "time", types.SimpleNamespace(time=lambda: clock[0]))
    store = SessionStore(tmp_path)
    session_id = store.resolve("Bearer " + store.create()["session_token"], required=True)
    for _ in range(20):
        store.reserve(session_id)
    assert store.usage() == {
        "limit": 20,
        "used": 20,
        "remaining": 0,
        "day": "2026-10-17",
        "resets_at": stamp(16),
        "timezone": "Asia/Singapore",
    }
    clock[0] = stamp(16)  # 00:00 on 18 October in Singapore.
    assert store.usage()["remaining"] == 20
    store.reserve(session_id)  # An existing browser can use the new day's allowance.
    assert store.usage()["used"] == 1
    assert store.info(session_id)["runs"] == 21
    clock[0] = stamp(0, day=18)  # UTC midnight is not another Singapore day.
    assert store.usage()["remaining"] == 19
    assert store.usage()["day"] == "2026-10-18"


def test_expired_or_forged_sessions_cannot_resume_and_storage_fails_closed(tmp_path):
    store = SessionStore(tmp_path)
    token = store.create()["session_token"]
    session_id = store.resolve("Bearer " + token, required=True)
    store.reserve(session_id)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE sessions SET expires = 0 WHERE id = ?", (session_id,))
    for candidate in [token, "a" * 43, "forged"]:
        with pytest.raises(HTTPException) as error:
            store.resolve("Bearer " + candidate, required=True)
        assert error.value.status_code == 401
    assert store.resolve(None, required=False) is None
    assert store.create()["usage"]["remaining"] == 19
    store.path.unlink()
    store.path.mkdir()
    with pytest.raises(HTTPException) as error:
        store.reserve(session_id)
    assert error.value.status_code == 503


@pytest.mark.skipif(shutil.which("node") is None, reason="Frontend build uses Node.js")
def test_vercel_build_only_embeds_public_backend_origin(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "web").mkdir()
    shutil.copy(root / "scripts/build-web.mjs", tmp_path / "scripts/build-web.mjs")
    shutil.copy(root / "web/index.html", tmp_path / "web/index.html")
    monkeypatch.setenv("RTL_AGENT_API_BASE_URL", "https://backend.hf.space/")
    monkeypatch.setenv("OPENAI_API_KEY", "must-never-be-in-browser-build")
    result = subprocess.run(
        ["node", "scripts/build-web.mjs"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    page = (tmp_path / "dist/index.html").read_text()
    assert '{"apiBaseUrl":"https://backend.hf.space"}' in page
    assert "must-never-be-in-browser-build" not in page
    assert list((tmp_path / "dist").iterdir()) == [tmp_path / "dist/index.html"]
    for invalid in [
        "http://backend.hf.space",
        "https://user:pass@backend.hf.space",
        "https://backend.hf.space/api",
        "https://backend.hf.space?key=private",
    ]:
        monkeypatch.setenv("RTL_AGENT_API_BASE_URL", invalid)
        result = subprocess.run(
            ["node", "scripts/build-web.mjs"], cwd=tmp_path, capture_output=True, text=True
        )
        assert result.returncode != 0
        assert invalid not in result.stderr


def test_backend_deploy_uploads_only_allowlisted_files_without_openai_credentials(monkeypatch):
    root = Path(__file__).parents[1]
    captured = {}

    class FakeAPI:
        def __init__(self, token):
            captured["token"] = token

        def upload_folder(self, **kwargs):
            files = Path(kwargs.pop("folder_path"))
            names = {str(path.relative_to(files)) for path in files.rglob("*") if path.is_file()}
            assert "Dockerfile" in names and "src/rtl_agent/hosting.py" in names
            assert "web/index.html" in names
            assert not any(
                ".env" in name or name.startswith(("runs/", ".git/", "tests/")) for name in names
            )
            assert "sdk: docker" in (files / "README.md").read_text()
            assert all("openai-secret-canary" not in (files / name).read_text() for name in names)
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(HfApi=FakeAPI))
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-canary")
    deploy = runpy.run_path(str(root / "scripts/deploy-backend.py"))["deploy"]
    deploy("owner/rtl-agent", "fixture-scoped-hf-token")
    assert captured["token"] == "fixture-scoped-hf-token"
    assert captured["repo_id"] == "owner/rtl-agent" and captured["repo_type"] == "space"
    assert captured["delete_patterns"] == ["*"]
