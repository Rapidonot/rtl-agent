import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

pytest.importorskip("fastapi")

from rtl_agent.models import RTLRepair, SynthesisMetrics, ToolResult
from rtl_agent.models import TestbenchDraft as BenchDraft
from rtl_agent.providers import Provider
from rtl_agent.web import create_app

VCD = """$timescale 1ps $end
$scope module tb $end
$var reg 1 ! clk $end
$var wire 8 " result [7:0] $end
$upscope $end
$enddefinitions $end
#0
0!
bx "
#10000
1!
b11111111 "
"""


class FakeTools:
    hold = None
    reached = None

    def __init__(self, config):
        self.config = config
        self.on_result = None
        self.command_timeout = lambda: config.timeout_seconds

    def verify(self, directory, rtl, tb):
        failed = directory.name == "01"
        stages = ["lint", "compile", "simulate"] + ([] if failed else ["synth"])
        results, metrics = [], None
        for stage in stages:
            self.command_timeout()
            if stage == "compile" and self.hold is not None:
                self.reached.set()
                self.hold.wait(2)
            output = "RTL_AGENT_PASS\n" if stage == "simulate" else ""
            if stage == "simulate":
                (directory / "waves.vcd").write_text(VCD)
                if failed:
                    output = (
                        "FATAL: tb.sv:9: mismatch expected -18 actual 65518\n"
                        "       Time: 10000 Scope: tb\n"
                    )
            if stage == "synth":
                metrics = SynthesisMetrics(total_cells=1, combinational_gates=1)
                (directory / "netlist.v").write_text("// measured test fixture")
            result = ToolResult(
                stage=stage,
                command=[stage],
                returncode=int(failed and stage == "simulate"),
                duration_seconds=0.02,
                output=output,
                passed=not (failed and stage == "simulate"),
            )
            if not result.passed:
                from rtl_agent.models import Diagnostic

                result.diagnostics.append(
                    Diagnostic(
                        tool=stage, severity="error", message="mismatch expected -18 actual 65518"
                    )
                )
            results.append(result)
            if self.on_result:
                self.on_result(result, metrics)
        return results, metrics


@pytest.fixture
def setup_web(tmp_path, monkeypatch, design):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-server-key")
    monkeypatch.setenv("RTL_AGENT_MODEL", "test-model")
    monkeypatch.delenv("RTL_AGENT_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setattr(
        "rtl_agent.web.tool_paths",
        lambda: {x: "/bin/true" for x in ("verilator", "iverilog", "vvp", "yosys")},
    )
    monkeypatch.setattr("rtl_agent.agent.EDAToolchain", FakeTools)
    monkeypatch.setattr(FakeTools, "hold", None)
    provider = Mock(spec=Provider)
    provider.name, provider.model = "mock", "test-model"
    provider.generate_testbench.side_effect = lambda *_: BenchDraft(
        assumptions=design.assumptions, testbench=design.testbench
    )
    provider.generate.return_value = design
    provider.repair.return_value = RTLRepair(rtl=design.rtl, change_summary="Repair arithmetic.")
    provider.caption.return_value = "The test caught an incorrect number. The final attempt passed."
    app = create_app(runs_dir=tmp_path / "runs", provider_factory=lambda *_: provider)
    return app, provider


class LocalClient:
    def __init__(self, app, address="127.0.0.1"):
        self.app, self.address = app, address

    def __enter__(self):
        return self

    def __exit__(self, *args):
        active = self.app.state.manager.active
        if active:
            active.control.cancelled.set()
            active.thread.join(3)

    def request(self, method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=self.app, client=(self.address, 50000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


def client_for(app):
    return LocalClient(app)


def start(client, **fields):
    response = client.post(
        "/api/run",
        json={
            "spec": "Custom editable chip specification.",
            "top": "npu_neural_mac",
            "max_attempts": 3,
            **fields,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def finished(client, run_id):
    for _ in range(200):
        report = client.get(f"/api/run/{run_id}").json()
        if report["status"] != "running" and not report["caption_pending"]:
            return report
        time.sleep(0.01)
    pytest.fail("Run did not finish")


def test_health_examples_and_frontend_do_not_expose_key(setup_web):
    app, _ = setup_web
    with client_for(app) as client:
        response = client.get("/api/health")
        assert response.json()["live_available"]
        assert response.json()["api_key_set"]
        assert "secret-server-key" not in response.text
        examples = client.get("/api/examples").json()
        assert len(examples) == 5
        assert all(
            e["spec"] == (Path("examples") / (e["top"] + ".txt")).read_text() for e in examples
        )
        html = client.get("/")
        assert html.status_code == 200
        assert "RemoteRunEngine" in html.text and "mode-replay" in html.text
        assert "secret-server-key" not in html.text


@pytest.mark.parametrize("attempts,expected", [(1, "failed"), (3, "passed")])
def test_gallery_lists_actual_local_results_without_source_or_logs(setup_web, attempts, expected):
    app, provider = setup_web
    with client_for(app) as client:
        assert client.get("/api/runs").json() == []
        run_id = start(client, max_attempts=attempts)
        finished(client, run_id)
        response = client.get("/api/runs")
        assert response.status_code == 200
        records = response.json()
        assert len(records) == 1 and records[0]["id"] == run_id
        assert records[0]["serverRun"] and records[0]["userSaved"]
        assert records[0]["report"]["status"] == expected
        for attempt in records[0]["report"]["attempts"]:
            assert set(attempt) == {"number", "status", "synthesis"}
        assert "secret-server-key" not in response.text
        assert "rtl" not in records[0]["report"] and "log" not in records[0]["report"]
        provider.generate_testbench.assert_called_once()


def test_gallery_description_is_complete_and_does_not_repeat_raw_model_prose(setup_web, design):
    app, _ = setup_web
    design.summary = "Implemented npu_neural_mac with cpu_alu8 and RTL code. " * 20
    with client_for(app) as client:
        run_id = start(client, spec="Design a signed INT8 multiply-accumulate block.")
        report = finished(client, run_id)
        record = client.get("/api/runs").json()[0]
        expected = "Multiplies two inputs and adds each result to a running total."
        assert record["description"] == expected
        assert record["report"]["description"] == report["description"] == expected
        assert "npu_neural_mac" not in record["description"]


def test_gallery_and_artifacts_survive_restart_without_a_key(setup_web, monkeypatch):
    app, provider = setup_web
    with client_for(app) as client:
        run_id = start(client)
        before = finished(client, run_id)
    monkeypatch.delenv("OPENAI_API_KEY")
    restarted = create_app(runs_dir=app.state.manager.root)
    with client_for(restarted) as client:
        assert not client.get("/api/health").json()["live_available"]
        records = client.get("/api/runs").json()
        assert records[0]["id"] == run_id
        restored = client.get(f"/api/run/{run_id}").json()
        assert restored["status"] == before["status"] == "passed"
        assert restored["attempts"][-1]["rtl"] == before["attempts"][-1]["rtl"]
        wave = client.get(f"/api/run/{run_id}/attempts/2/waveform").json()
        assert wave["duration_ns"] == 10
    provider.generate.assert_called_once()


def test_gallery_skips_broken_reports_and_symlinks_outside_runs(setup_web, tmp_path):
    app, _ = setup_web
    with client_for(app) as client:
        run_id = start(client)
        finished(client, run_id)
        broken = app.state.manager.root / "20260101T000000Z-11111111"
        broken.mkdir()
        (broken / "report.json").write_text("not valid json")
        private = tmp_path / "outside"
        private.mkdir()
        (private / "report.json").write_text('{"private": "never-show-this"}')
        (app.state.manager.root / "20260101T000000Z-22222222").symlink_to(private)
        response = client.get("/api/runs")
        assert [r["id"] for r in response.json()] == [run_id]
        assert "never-show-this" not in response.text


def test_gallery_does_not_save_a_failed_attempt_before_the_live_run_finishes(setup_web):
    app, provider = setup_web
    hold, reached = threading.Event(), threading.Event()

    def repair(*_):
        reached.set()
        hold.wait(2)
        return provider.repair.return_value

    provider.repair.side_effect = repair
    with client_for(app) as client:
        try:
            run_id = start(client)
            assert reached.wait(1)
            assert client.get("/api/runs").json() == []
        finally:
            hold.set()
        finished(client, run_id)
        assert client.get("/api/runs").json()[0]["report"]["status"] == "passed"


def test_run_returns_early_conflicts_and_completes_with_sources_and_caption(setup_web):
    app, provider = setup_web
    hold = threading.Event()
    provider.generate.side_effect = lambda *args: (hold.wait(2), provider.generate.return_value)[1]
    with client_for(app) as client:
        try:
            started = time.monotonic()
            run_id = start(client, max_attempts=99)
            assert time.monotonic() - started < 1
            report = client.get(f"/api/run/{run_id}").json()
            assert report["status"] == "running" and report["max_attempts"] == 5
            assert report["spec"] == "Custom editable chip specification."
            assert client.post("/api/run", json={"spec": "other", "top": "dut"}).status_code == 409
        finally:
            hold.set()
        report = finished(client, run_id)
        assert report["status"] == "passed"
        assert [a["status"] for a in report["attempts"]] == ["failed", "passed"]
        assert report["source"] == "live" and report["model"] == "test-model"
        assert report["title"] == "Custom chip block" and report["category"] == "Custom"
        assert report["caption_source"] == "model"
        assert "secret-server-key" not in json.dumps(report)
        provider.caption.assert_called_once()
        facts = provider.caption.call_args.args[0]
        assert set(facts) == {
            "spec",
            "diagnostics",
            "change_summary",
            "attempt_number",
            "total_attempts",
            "final_status",
            "later_attempt_passed",
            "failure_kind",
        }
        assert facts["change_summary"] == "Repair arithmetic."
        assert facts["final_status"] == "passed" and facts["diagnostics"]
        assert facts["attempt_number"] == 1 and facts["later_attempt_passed"]
        for attempt in report["attempts"]:
            assert attempt["rtl"] and attempt["testbench"]
            assert attempt["testbench_sha256"] == report["testbench_sha256"]
            wave = client.get(f"/api/run/{run_id}/attempts/{attempt['number']}/waveform").json()
            assert wave["duration_ns"] == 10
            assert wave["channels"][1]["samples"] == [[0, "bx"], [10, 255]]
        assert report["attempts"][0]["diagnostic"]["time_ns"] == 10


def test_stage_progress_is_saved_without_marking_partial_attempt_passed(setup_web, monkeypatch):
    app, _ = setup_web
    hold, reached = threading.Event(), threading.Event()
    monkeypatch.setattr(FakeTools, "hold", hold)
    monkeypatch.setattr(FakeTools, "reached", reached)
    with client_for(app) as client:
        try:
            run_id = start(client)
            assert reached.wait(1)
            report = client.get(f"/api/run/{run_id}").json()
            assert report["attempts"][0]["status"] == "running"
            assert report["attempts"][0]["gates"]["lint"] == "pass"
            assert report["attempts"][0]["gates"]["synth"] == "off"
            native = json.loads((app.state.manager.root / run_id / "report.json").read_text())
            assert len(native["current_attempt"]["tools"]) == 1
            assert not native["current_attempt"]["passed"]
        finally:
            hold.set()
        finished(client, run_id)


@pytest.mark.parametrize("number,expected", [(0, 1), (-10, 1), (100, 5)])
def test_attempt_limits_are_clamped(setup_web, number, expected):
    app, _ = setup_web
    with client_for(app) as client:
        report = finished(client, start(client, max_attempts=number))
        assert report["max_attempts"] == expected
        assert len(report["attempts"]) <= expected


@pytest.mark.parametrize(
    "payload",
    [
        {"spec": "", "top": "dut"},
        {"spec": "x" * 65537, "top": "dut"},
        {"spec": "x", "top": "../escape"},
        {"spec": "x", "top": "tb"},
        {"spec": "x", "top": "dut", "api_key": "secret-server-key"},
    ],
)
def test_invalid_inputs_fail_before_provider_call(setup_web, payload):
    app, provider = setup_web
    with client_for(app) as client:
        response = client.post("/api/run", json=payload)
        assert response.status_code == 422
        assert "secret-server-key" not in response.text
        provider.generate.assert_not_called()


def test_key_tools_and_model_are_required_before_live_run(setup_web, monkeypatch):
    app, provider = setup_web
    with client_for(app) as client:
        monkeypatch.delenv("OPENAI_API_KEY")
        assert not client.get("/api/health").json()["live_available"]
        assert client.post("/api/run", json={"spec": "x", "top": "dut"}).status_code == 503
        monkeypatch.setenv("OPENAI_API_KEY", "secret-server-key")
        monkeypatch.setattr("rtl_agent.web.tool_paths", lambda: {"yosys": None})
        assert "yosys" in client.get("/api/health").json()["reason"]
        monkeypatch.delenv("RTL_AGENT_MODEL")
        assert client.post("/api/run", json={"spec": "x", "top": "dut"}).status_code == 503
        provider.generate.assert_not_called()


def test_local_host_and_origin_guards(setup_web):
    app, _ = setup_web
    with client_for(app) as client:
        assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403
        assert (
            client.post(
                "/api/run",
                headers={"Origin": "http://evil.example"},
                json={"spec": "x", "top": "dut"},
            ).status_code
            == 403
        )
        assert client.get("/api/health", headers={"Origin": "null"}).status_code == 403
    with LocalClient(app, "203.0.113.2") as remote:
        assert remote.get("/api/health").status_code == 403


def test_run_ids_and_symlink_artifacts_cannot_escape_runs(setup_web, tmp_path):
    app, _ = setup_web
    with client_for(app) as client:
        run_id = start(client)
        finished(client, run_id)
        assert client.get("/api/run/not-a-run").status_code == 404
        assert client.get("/api/run/%2e%2e%2fsecret").status_code == 404
        assert client.get(f"/api/run/{run_id}/attempts/0/waveform").status_code == 404
        outside = tmp_path / "secret.vcd"
        outside.write_text("private-outside-runs")
        path = app.state.manager.root / run_id / "attempts/01/waves.vcd"
        path.unlink()
        path.symlink_to(outside)
        response = client.get(f"/api/run/{run_id}/attempts/1/waveform")
        assert response.status_code in (404, 422)
        assert "private-outside-runs" not in response.text


def test_wall_timeout_keeps_worker_lock_until_the_operation_returns(setup_web):
    app, provider = setup_web
    hold = threading.Event()
    provider.generate.side_effect = lambda *args: (hold.wait(2), provider.generate.return_value)[1]
    app.state.manager.timeout = 0.1
    with client_for(app) as client:
        try:
            run_id = start(client)
            time.sleep(0.12)
            report = client.get(f"/api/run/{run_id}").json()
            assert report["status"] == "interrupted" and "timeout" in report["error"]
            assert client.post("/api/run", json={"spec": "other", "top": "dut"}).status_code == 409
        finally:
            hold.set()
        report = finished(client, run_id)
        assert report["attempts"] == []
        provider.repair.assert_not_called()


def test_cancel_is_cooperative_and_preserves_run(setup_web):
    app, provider = setup_web
    hold = threading.Event()
    provider.generate.side_effect = lambda *args: (hold.wait(2), provider.generate.return_value)[1]
    with client_for(app) as client:
        try:
            run_id = start(client)
            assert client.post(f"/api/run/{run_id}/cancel").status_code == 202
            assert client.get(f"/api/run/{run_id}").json()["status"] == "interrupted"
        finally:
            hold.set()
        assert finished(client, run_id)["status"] == "interrupted"


def test_missing_vcd_and_unparseable_time_stay_unknown(setup_web):
    app, _ = setup_web
    with client_for(app) as client:
        run_id = start(client)
        finished(client, run_id)
        path = app.state.manager.root / run_id / "attempts/01/waves.vcd"
        path.unlink()
        report = client.get(f"/api/run/{run_id}").json()
        assert report["attempts"][0]["diagnostic"]["time_ns"] is None
        assert client.get(f"/api/run/{run_id}/attempts/1/waveform").json() is None


@pytest.mark.parametrize("cause", ["missing", "timescale", "backwards", "empty"])
def test_missing_final_waveform_explains_display_failure_without_changing_pass(setup_web, cause):
    app, _ = setup_web
    with client_for(app) as client:
        run_id = start(client)
        before = finished(client, run_id)
        path = app.state.manager.root / run_id / "attempts/02/waves.vcd"
        if cause == "missing":
            path.unlink()
        elif cause == "timescale":
            path.write_text(VCD.replace("$timescale 1ps $end\n", ""))
        elif cause == "backwards":
            path.write_text(VCD + "#1\n0!\n")
        else:
            path.write_text("")
        response = client.get(f"/api/run/{run_id}/attempts/2/waveform?explain=true")
        assert response.status_code == 200
        assert response.json()["waveform"] is None and response.json()["reason"]
        after = client.get(f"/api/run/{run_id}").json()
        assert after["status"] == before["status"] == "passed"
        assert [a["gates"] for a in after["attempts"]] == [a["gates"] for a in before["attempts"]]
        assert after["attempts"][-1]["status"] == "passed"


def test_caption_failure_does_not_change_verification(setup_web):
    app, provider = setup_web
    provider.caption.side_effect = RuntimeError("caption service unavailable")
    with client_for(app) as client:
        report = finished(client, start(client))
        assert report["status"] == "passed"
        assert report["caption_source"] == "deterministic"
        assert "gave 65,518 when the expected answer was -18" in report["caption"]
        provider.caption.assert_called_once()


def test_provider_errors_never_persist_the_key(setup_web):
    from rtl_agent.providers import ProviderError

    app, provider = setup_web
    provider.generate.side_effect = ProviderError("Request failed: secret-server-key")
    with client_for(app) as client:
        run_id = start(client)
        report = finished(client, run_id)
        assert report["status"] == "error"
        assert "secret-server-key" not in json.dumps(report)
        assert (
            "secret-server-key" not in (app.state.manager.root / run_id / "report.json").read_text()
        )
        provider.caption.assert_not_called()


def test_unexpected_worker_failure_is_reported_instead_of_polling_forever(setup_web, caplog):
    app, provider = setup_web
    provider.generate.side_effect = TypeError("internal failure")
    with client_for(app) as client:
        report = finished(client, start(client))
        assert report["status"] == "error"
        assert report["error"] == "Run stopped unexpectedly."
        assert "TypeError: internal failure" in caplog.text
        assert "Traceback (most recent call last)" in caplog.text


def test_worker_logs_redact_key_in_chained_exceptions_and_do_not_attach_original(setup_web, caplog):
    app, provider = setup_web

    def fail(*_):
        try:
            raise LookupError("inner secret-server-key")
        except LookupError as cause:
            raise TypeError("outer secret-server-key") from cause

    provider.generate.side_effect = fail
    with client_for(app) as client:
        report = finished(client, start(client))
        assert report["error"] == "Run stopped unexpectedly."
        assert "secret-server-key" not in caplog.text
        assert "LookupError: inner [redacted]" in caplog.text
        assert "TypeError: outer [redacted]" in caplog.text
        records = [r for r in caplog.records if r.getMessage().startswith("Live run worker failed")]
        assert len(records) == 1 and not records[0].exc_info


def test_provider_initialization_failure_logs_trace_without_leaking_key(setup_web, caplog):
    app, _ = setup_web

    def fail(*_):
        raise TypeError("provider initialization secret-server-key")

    app.state.manager.provider_factory = fail
    with client_for(app) as client:
        response = client.post("/api/run", json={"spec": "counter", "top": "dut"})
        assert response.status_code == 503
        assert "Could not start the run" in response.text
        assert "secret-server-key" not in response.text + caplog.text
        assert "TypeError: provider initialization [redacted]" in caplog.text


@pytest.mark.parametrize("value,expected", [(None, 12000), ("1024", 1024), ("65536", 65536)])
def test_default_web_provider_reads_token_budget_at_run_start(
    setup_web, monkeypatch, tmp_path, value, expected
):
    _, provider = setup_web
    if value is not None:
        monkeypatch.setenv("RTL_AGENT_MAX_OUTPUT_TOKENS", value)
    construction = Mock(return_value=provider)
    monkeypatch.setattr("rtl_agent.web.OpenAIProvider", construction)
    app = create_app(runs_dir=tmp_path / "token-runs")
    with client_for(app) as client:
        assert client.get("/api/health").json()["max_output_tokens"] == expected
        report = finished(client, start(client))
        assert report["status"] == "passed"
        construction.assert_called_once()
        assert construction.call_args.args == ("test-model",)
        assert construction.call_args.kwargs["max_output_tokens"] == expected
        assert construction.call_args.kwargs["control"] is not None


@pytest.mark.parametrize("value", ["1023", "65537", "not-a-number", ""])
def test_invalid_token_budget_disables_live_before_any_provider_call(setup_web, monkeypatch, value):
    app, provider = setup_web
    monkeypatch.setenv("RTL_AGENT_MAX_OUTPUT_TOKENS", value)
    with client_for(app) as client:
        ready = client.get("/api/health").json()
        assert not ready["live_available"] and ready["max_output_tokens"] is None
        assert "RTL_AGENT_MAX_OUTPUT_TOKENS" in ready["reason"]
        response = client.post("/api/run", json={"spec": "counter", "top": "dut"})
        assert response.status_code == 503
        provider.generate_testbench.assert_not_called()
        provider.generate.assert_not_called()


@pytest.fixture
def hosted_web(setup_web):
    from rtl_agent.hosting import HostedSettings

    original, provider = setup_web
    settings = HostedSettings(
        backend_host="backend.hf.space",
        frontend_origins=("https://rtl-demo.vercel.app",),
        password="test-live-access-password-32-characters",
        daily_runs=2,
        max_attempts=2,
        run_timeout=60,
    )
    app = create_app(
        runs_dir=original.state.manager.root, hosted=settings, provider_factory=lambda *_: provider
    )
    return app, provider, settings


def hosted_headers(password=None, **extra):
    return {
        "Host": "backend.hf.space",
        "Origin": "https://rtl-demo.vercel.app",
        **({"Authorization": "Bearer " + password} if password else {}),
        **extra,
    }


def test_hosted_health_is_public_but_does_not_expose_secrets(hosted_web):
    app, provider, settings = hosted_web
    with LocalClient(app, "203.0.113.2") as client:
        response = client.get("/api/health", headers=hosted_headers())
        assert response.status_code == 200 and response.json()["live_available"]
        assert response.json()["authentication_required"]
        assert not response.json()["authenticated"]
        assert response.json()["limits"]["max_attempts"] == 2
        assert response.headers["access-control-allow-origin"] == "https://rtl-demo.vercel.app"
        assert response.headers["cache-control"] == "no-store"
        assert settings.password not in response.text and "secret-server-key" not in response.text
        assert settings.password not in client.get("/", headers=hosted_headers()).text
        provider.generate_testbench.assert_not_called()


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/runs"),
        ("POST", "/api/run"),
        ("GET", "/api/run/unknown"),
        ("POST", "/api/run/unknown/cancel"),
        ("GET", "/api/run/unknown/attempts/1/waveform"),
    ],
)
def test_hosted_live_endpoints_require_access_before_any_work(hosted_web, method, path):
    app, provider, _ = hosted_web
    with LocalClient(app, "203.0.113.2") as client:
        response = client.request(method, path, headers=hosted_headers())
        assert response.status_code == 401
        assert response.headers["access-control-allow-origin"] == "https://rtl-demo.vercel.app"
        assert app.state.manager.active is None
        provider.generate_testbench.assert_not_called()


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "https://other.vercel.app",
        "https://rtl-demo.vercel.app.evil.example",
        "null",
        "http://rtl-demo.vercel.app",
    ],
)
def test_hosted_origins_are_exact_even_with_valid_access(hosted_web, origin):
    app, provider, settings = hosted_web
    with LocalClient(app, "203.0.113.2") as client:
        response = client.get("/api/runs", headers=hosted_headers(settings.password, Origin=origin))
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers
        provider.generate_testbench.assert_not_called()


def test_hosted_preflight_requires_origin_method_and_headers_but_no_password(hosted_web):
    app, _, _ = hosted_web
    with LocalClient(app, "203.0.113.2") as client:
        headers = hosted_headers(
            **{
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            }
        )
        response = client.request("OPTIONS", "/api/run", headers=headers)
        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == headers["Origin"]
        assert "access-control-allow-credentials" not in response.headers
        headers["Access-Control-Request-Headers"] = "X-Unsafe"
        assert client.request("OPTIONS", "/api/run", headers=headers).status_code == 403
        headers["Access-Control-Request-Headers"] = "authorization"
        headers["Access-Control-Request-Method"] = "DELETE"
        assert client.request("OPTIONS", "/api/run", headers=headers).status_code == 403


def test_hosted_authenticated_run_has_unchanged_pass_and_freeze_and_durable_quota(hosted_web):
    app, provider, settings = hosted_web
    headers = hosted_headers(settings.password)
    with LocalClient(app, "203.0.113.2") as client:
        for _ in range(2):
            response = client.post(
                "/api/run",
                headers=headers,
                json={
                    "spec": "Design a multiply-accumulate block.",
                    "top": "npu_neural_mac",
                    "max_attempts": 5,
                },
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            for _ in range(200):
                report = client.get("/api/run/" + run_id, headers=headers).json()
                if report["status"] != "running" and not report["caption_pending"]:
                    break
                time.sleep(0.01)
            assert report["status"] == "passed" and report["max_attempts"] == 2
            assert [a["status"] for a in report["attempts"]] == ["failed", "passed"]
            assert len({a["testbench_sha256"] for a in report["attempts"]}) == 1
            wave = client.get(f"/api/run/{run_id}/attempts/1/waveform", headers=headers).json()
            assert wave["duration_ns"] == 10
        assert len(client.get("/api/runs", headers=headers).json()) == 2
        assert (
            client.post(
                "/api/run", headers=headers, json={"spec": "third", "top": "dut"}
            ).status_code
            == 429
        )
    restarted = create_app(
        runs_dir=app.state.manager.root, hosted=settings, provider_factory=lambda *_: provider
    )
    with LocalClient(restarted, "203.0.113.2") as client:
        assert (
            client.post(
                "/api/run", headers=headers, json={"spec": "third", "top": "dut"}
            ).status_code
            == 429
        )
    assert provider.generate_testbench.call_count == 2


def test_hosted_bad_access_is_throttled_but_valid_access_still_works(hosted_web):
    app, _, settings = hosted_web
    with LocalClient(app, "203.0.113.2") as client:
        for _ in range(20):
            assert client.get("/api/health", headers=hosted_headers("wrong")).status_code == 401
        response = client.get("/api/health", headers=hosted_headers("wrong"))
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        assert client.get("/api/health", headers=hosted_headers()).status_code == 200
        unlocked = client.get("/api/health", headers=hosted_headers(settings.password))
        assert unlocked.status_code == 200 and unlocked.json()["authenticated"]


def test_hosted_worker_logs_redact_access_password_and_api_key(hosted_web, caplog):
    app, _, settings = hosted_web
    app.state.manager.provider_factory = Mock(
        side_effect=RuntimeError("secret-server-key " + settings.password)
    )
    with LocalClient(app, "203.0.113.2") as client:
        response = client.post(
            "/api/run", headers=hosted_headers(settings.password), json={"spec": "x", "top": "dut"}
        )
        assert response.status_code == 503
        active = app.state.manager.active
        if active:
            active.thread.join(2)
    assert settings.password not in caplog.text and "secret-server-key" not in caplog.text
    assert "RuntimeError" in caplog.text and "[redacted]" in caplog.text
