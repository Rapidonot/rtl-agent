"""Localhost-only web UI and background run API. Install the optional web extra."""

import ipaddress
import json
import logging
import os
import re
import threading
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, field_validator

from rtl_agent.agent import RTLAgent, _save, load_report
from rtl_agent.captions import circuit_name
from rtl_agent.control import RunControl, RunStopped
from rtl_agent.models import RunConfig, RunReport
from rtl_agent.policy import validate_top
from rtl_agent.providers import OpenAIProvider, max_output_tokens_from_env
from rtl_agent.tools import tool_paths
from rtl_agent.web_artifacts import (
    EXAMPLES,
    artifact,
    gallery_description,
    read_text,
    ui_report,
    waveform_with_reason,
)

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def resource_dir(name: str, packaged: str) -> Path:
    bundled = Path(__file__).parent / packaged
    return bundled if bundled.is_dir() else Path(__file__).resolve().parents[2] / name


def health() -> dict:
    tools = {name: path is not None for name, path in tool_paths().items()}
    key = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    model = os.environ.get("RTL_AGENT_MODEL", "").strip()
    reasons = [] if key else ["Set OPENAI_API_KEY in the server terminal."]
    missing = [name for name, found in tools.items() if not found]
    if missing:
        reasons.append("Missing tools: " + ", ".join(missing) + ".")
    if not model:
        reasons.append("Set RTL_AGENT_MODEL to an API model ID.")
    try:
        tokens = max_output_tokens_from_env()
    except ValueError as exc:
        tokens = None
        reasons.append(str(exc))
    return {
        "api_key_set": key,
        "tools": tools,
        "model": model or None,
        "max_output_tokens": tokens,
        "live_available": not reasons,
        "reason": " ".join(reasons),
    }


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: str
    top: str
    max_attempts: int = 3

    @field_validator("spec")
    @classmethod
    def valid_spec(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > 65536:
            raise ValueError("Specification must contain 1–65536 UTF-8 bytes.")
        return value.strip()

    @field_validator("top")
    @classmethod
    def valid_top(cls, value: str) -> str:
        validate_top(value)
        return value

    @field_validator("max_attempts")
    @classmethod
    def clamp_attempts(cls, value: int) -> int:
        return max(1, min(5, value))


@dataclass
class Job:
    request: RunRequest
    control: RunControl
    ready: threading.Event = field(default_factory=threading.Event)
    run_id: str | None = None
    thread: threading.Thread | None = None
    error: str | None = None


class RunManager:
    def __init__(self, root: Path, timeout: float, provider_factory=None):
        self.root, self.timeout = root.resolve(), timeout
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider_factory = provider_factory or (
            lambda model, control: OpenAIProvider(
                model, control=control, max_output_tokens=max_output_tokens_from_env()
            )
        )
        self.lock = threading.Lock()
        self.active: Job | None = None
        self.jobs: dict[str, Job] = {}

    def start(self, request: RunRequest) -> str:
        with self.lock:
            if self.active is not None:
                raise HTTPException(
                    409, "A run is already active. Wait for it to finish or stop it."
                )
            job = Job(request=request, control=RunControl(self.timeout))
            self.active = job
        api_key = os.environ.get("OPENAI_API_KEY", "")

        def created(report):
            job.run_id = report.run_id
            with self.lock:
                self.jobs[report.run_id] = job
            job.ready.set()

        def work():
            try:
                provider = self.provider_factory(os.environ["RTL_AGENT_MODEL"], job.control)
                config = RunConfig(
                    top=request.top, max_attempts=request.max_attempts, output_dir=self.root
                )
                RTLAgent(provider, config, control=job.control, captions=True).run(
                    request.spec, on_created=created
                )
            except Exception:
                # Redact the full chained traceback before passing it to logging.
                # Do not attach the original exception: a formatter could expose it again.
                details = traceback.format_exc()
                for key in {api_key, os.environ.get("OPENAI_API_KEY", "")} - {""}:
                    for form in (key, repr(key)[1:-1], json.dumps(key)[1:-1]):
                        details = details.replace(form, "[redacted]")
                logging.exception("Live run worker failed:\n%s", details, exc_info=False)
                # Never serialize exception bodies from providers or credentials.
                job.error = (
                    "Could not start the run. Check the server environment and runs directory."
                )
                if job.run_id:
                    try:
                        report = load_report(artifact(self.root, job.run_id, "report.json"))
                        if report.status == "running":
                            report.status, report.error = "error", "Run stopped unexpectedly."
                            _save(report)
                    except (OSError, ValueError):
                        pass
            finally:
                job.ready.set()
                with self.lock:
                    if self.active is job:
                        self.active = None

        job.thread = threading.Thread(target=work, daemon=True, name="rtl-run")
        job.thread.start()
        if not job.ready.wait(3):
            job.control.cancelled.set()
            raise HTTPException(503, "Run initialization timed out. Please retry once it stops.")
        if job.run_id is None:
            raise HTTPException(503, job.error or "Could not initialize run.")
        return job.run_id

    def report(self, run_id: str) -> dict:
        path = artifact(self.root, run_id, "report.json")
        if not path.is_file():
            raise HTTPException(404, "Run not found.")
        report = load_report(path)
        config_path = artifact(self.root, run_id, "config.json")
        config = json.loads(read_text(config_path) or "{}")
        job = self.jobs.get(run_id)
        if job is None and report.caption_pending:
            report = report.model_copy(update={"caption_pending": False})
        if report.status == "running":
            reason = None
            if job:
                try:
                    job.control.check()
                except RunStopped as exc:
                    reason = str(exc)
            else:
                reason = "Server restarted; this unfinished run is no longer active."
            if reason:
                report = report.model_copy(update={"status": "interrupted", "error": reason})
        reference = None
        if report.top in EXAMPLES:
            reference = read_text(resource_dir("examples", "example_specs") / f"{report.top}.txt")
        known = reference is not None and reference.strip() == report.spec.strip()
        result = ui_report(report, self.root, config.get("max_attempts", 3), known_example=known)
        result["worker_active"] = bool(job and job.thread and job.thread.is_alive())
        return result

    def gallery(self) -> list[dict]:
        """Read completed local runs without loading their source or VCD artifacts."""
        records = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            try:
                path = artifact(self.root, directory.name, "report.json")
                text = read_text(path, limit=16 * 1024 * 1024)
                if text is None:
                    continue
                report = RunReport.model_validate_json(text)
                if report.run_id != directory.name or not report.attempts:
                    continue
                # A previous process may have stopped before writing a terminal status.
                if report.status == "running":
                    job = self.jobs.get(report.run_id)
                    if job and job.thread and job.thread.is_alive():
                        continue
                    report = report.model_copy(update={"status": "interrupted"})
                name = circuit_name(report.spec)
                if name == "circuit":
                    name = (
                        re.sub(
                            r"^(?:please\s+)?(?:design|build|create|implement|make)\s+(?:(?:a|an|the)\s+)?",
                            "",
                            report.spec.splitlines()[0].strip(),
                            flags=re.I,
                        ).strip(" .`")
                        or "Custom chip block"
                    )
                category, title = "Custom", name[0].upper() + name[1:80]
                if report.top in EXAMPLES:
                    reference = read_text(
                        resource_dir("examples", "example_specs") / f"{report.top}.txt"
                    )
                    if reference is not None and reference.strip() == report.spec.strip():
                        category, title = EXAMPLES[report.top]
                records.append(
                    {
                        "id": report.run_id,
                        "title": title,
                        "category": category,
                        "description": gallery_description(report.spec),
                        "userSaved": True,
                        "serverRun": True,
                        "report": {
                            "run_id": report.run_id,
                            "top": report.top,
                            "model": report.model,
                            "source": "live",
                            "status": report.status,
                            "started_at": report.started_at,
                            "description": gallery_description(report.spec),
                            "attempts": [
                                {
                                    "number": a.number,
                                    "status": "passed" if a.passed else "failed",
                                    "synthesis": a.synthesis.model_dump() if a.synthesis else None,
                                }
                                for a in report.attempts
                            ],
                        },
                    }
                )
            except (OSError, ValueError, IndexError):
                continue  # A broken or out-of-root artifact cannot hide valid history.
            if len(records) == 100:
                break
        return sorted(records, key=lambda record: record["report"]["started_at"], reverse=True)


def create_app(
    *, runs_dir: Path = Path("runs"), run_timeout: float = 600, provider_factory=None
) -> FastAPI:
    if not 0 < run_timeout <= 1800:
        raise ValueError("Run timeout must be between 0 and 1800 seconds.")
    manager = RunManager(runs_dir, run_timeout, provider_factory)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if manager.active:
            manager.active.control.cancelled.set()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.manager = manager

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        try:
            remote = ipaddress.ip_address(request.client.host)
            hostname = urlsplit("http://" + request.headers.get("host", "")).hostname
            allowed = remote.is_loopback and hostname in LOCAL_HOSTS
        except (ValueError, AttributeError):
            allowed = False
        origin = request.headers.get("origin")
        if origin and origin != f"http://{request.headers.get('host', '')}":
            allowed = False
        if not allowed:
            return JSONResponse({"detail": "Local same-origin requests only."}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            body = b"".join([chunk async for chunk in response.body_iterator])
            key = os.environ.get("OPENAI_API_KEY", "")
            if key:
                body = body.replace(key.encode(), b"[redacted]")
                escaped = json.dumps(key)[1:-1].encode()
                body = body.replace(escaped, b"[redacted]")
            headers = {k: v for k, v in response.headers.items() if k != "content-length"}
            headers["Cache-Control"] = "no-store"
            return Response(body, status_code=response.status_code, headers=headers)
        return response

    @app.get("/")
    def index():
        return FileResponse(
            resource_dir("web", "web_assets") / "index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/health")
    def get_health():
        result = health()
        result["busy"] = manager.active is not None
        return result

    @app.get("/api/examples")
    def examples():
        directory = resource_dir("examples", "example_specs")
        return [
            {
                "id": top,
                "top": top,
                "category": category,
                "title": title,
                "spec": (directory / f"{top}.txt").read_text(encoding="utf-8"),
            }
            for top, (category, title) in EXAMPLES.items()
        ]

    @app.get("/api/runs")
    def saved_runs():
        return manager.gallery()

    @app.post("/api/run", status_code=202)
    def start_run(request: RunRequest):
        ready = health()
        if not ready["live_available"]:
            raise HTTPException(503, ready["reason"])
        return {"run_id": manager.start(request)}

    @app.get("/api/run/{run_id}")
    def get_run(run_id: str):
        try:
            return manager.report(run_id)
        except (ValueError, OSError):
            raise HTTPException(404, "Run report is unavailable or invalid.") from None

    @app.post("/api/run/{run_id}/cancel", status_code=202)
    def cancel_run(run_id: str):
        try:
            artifact(manager.root, run_id)
        except ValueError:
            raise HTTPException(404, "Run not found.") from None
        job = manager.jobs.get(run_id)
        if job is None:
            raise HTTPException(404, "Active run not found.")
        job.control.cancelled.set()
        return {
            "run_id": run_id,
            "message": "Stop requested; finishing the current bounded operation.",
        }

    @app.get("/api/run/{run_id}/attempts/{number}/waveform")
    def get_waveform(run_id: str, number: int, explain: bool = False):
        if not 1 <= number <= 20:
            raise HTTPException(404, "Attempt not found.")
        try:
            # Resolve report first, so a VCD can only be served for a known attempt.
            report = manager.report(run_id)
            if not any(a["number"] == number for a in report["attempts"]):
                raise HTTPException(404, "Attempt not found.")
            wave, reason = waveform_with_reason(
                artifact(manager.root, run_id, "attempts", f"{number:02d}", "waves.vcd")
            )
            return {"waveform": wave, "reason": reason} if explain else wave
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError:
            raise HTTPException(404, "Waveform unavailable.") from None

    return app
