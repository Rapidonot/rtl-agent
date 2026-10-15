"""Validated provider responses and serializable run artifacts."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DesignDraft(StrictModel):
    summary: str
    assumptions: list[str]
    rtl: str
    testbench: str


class RTLRepair(StrictModel):
    change_summary: str
    rtl: str


class RunConfig(StrictModel):
    top: str = Field(default="dut", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    max_attempts: int = Field(default=3, ge=1, le=20)
    timeout_seconds: float = Field(default=10.0, gt=0, le=600)
    max_log_bytes: int = Field(default=262144, ge=1024, le=4194304)
    output_dir: Path = Path("runs")


class Diagnostic(StrictModel):
    tool: str
    severity: Literal["error", "warning", "info"]
    message: str
    file: str | None = None
    line: int | None = None
    code: str | None = None


class ToolResult(StrictModel):
    stage: str
    command: list[str]
    returncode: int | None
    duration_seconds: float
    output: str
    passed: bool
    timed_out: bool = False
    output_limited: bool = False
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class Attempt(StrictModel):
    number: int
    rtl_sha256: str
    testbench_sha256: str
    change_summary: str
    passed: bool
    tools: list[ToolResult]
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class RunReport(StrictModel):
    schema_version: int = 1
    run_id: str
    provider: str
    model: str
    spec: str
    top: str
    started_at: str
    finished_at: str | None = None
    status: Literal["running", "passed", "failed", "error", "interrupted"] = "running"
    summary: str = ""
    assumptions: list[str] = Field(default_factory=list)
    testbench_sha256: str | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    error: str | None = None
    run_dir: str

    @property
    def passed(self) -> bool:
        return self.status == "passed"
