"""Hosted configuration and quotas never need model or external network access."""

import runpy
import shutil
import sqlite3
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException

from rtl_agent.hosting import DailyQuota, HostedSettings


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
        {"password": "short"},
        {"password": "x" * 257},
        {"password": "x" * 30 + "\n"},
        {"password": "é" * 30},
        {"daily_runs": 0},
        {"daily_runs": 101},
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
        password="test-password-for-invited-visitors",
    )
    with pytest.raises(ValueError):
        HostedSettings(**(valid | fields))


def test_hosted_env_is_explicit_and_password_is_not_in_repr(monkeypatch):
    monkeypatch.setenv("RTL_AGENT_PUBLIC_HOST", "backend.hf.space")
    monkeypatch.setenv(
        "RTL_AGENT_ALLOWED_ORIGINS", "https://site.vercel.app/, https://site.vercel.app"
    )
    monkeypatch.setenv("RTL_AGENT_LIVE_PASSWORD", "test-password-for-invited-visitors")
    settings = HostedSettings.from_env()
    assert settings.frontend_origins == ("https://site.vercel.app",)
    assert settings.password not in repr(settings)
    monkeypatch.setenv("RTL_AGENT_DAILY_RUN_LIMIT", "not-an-integer")
    with pytest.raises(ValueError, match="must be an integer"):
        HostedSettings.from_env()


def test_daily_quota_reservations_are_atomic_and_persist(tmp_path):
    quota = DailyQuota(tmp_path, 2)
    barrier = threading.Barrier(8)

    def reserve(_):
        barrier.wait()
        try:
            quota.reserve()
            return True
        except HTTPException as exc:
            assert exc.status_code == 429
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(reserve, range(8))).count(True) == 2
    with pytest.raises(HTTPException) as error:
        DailyQuota(tmp_path, 2).reserve()
    assert error.value.status_code == 429


def test_daily_quota_resets_on_new_utc_day_and_fails_closed(tmp_path):
    quota = DailyQuota(tmp_path, 1)
    with sqlite3.connect(quota.path) as db:
        db.execute("INSERT INTO usage VALUES ('2000-01-01', 100)")
    quota.reserve()
    quota.path.unlink()
    quota.path.mkdir()
    with pytest.raises(HTTPException) as error:
        quota.reserve()
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
    monkeypatch.setenv("RTL_AGENT_LIVE_PASSWORD", "must-never-embed-this-password")
    result = subprocess.run(
        ["node", "scripts/build-web.mjs"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    page = (tmp_path / "dist/index.html").read_text()
    assert '{"apiBaseUrl":"https://backend.hf.space"}' in page
    assert "must-never-be-in-browser-build" not in page
    assert "must-never-embed-this-password" not in page
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
