# Validation — 18 October 2026

- `pytest -q`: **220 passed** 
- Ruff lint passes for `src`, `tests` and `scripts`; changed Python files pass
  formatting checks. JavaScript passes Node syntax checks.
- Hosted browser checks use separate website/backend origins, a fixture provider
  and real Verilator, Icarus and Yosys. They cover automatic session creation,
  reload persistence, session ownership, a failed first attempt, frozen-testbench
  repair, both actual waveforms, the gallery and per-run attempt limits. Twenty
  jobs across two browsers exhaust the shared daily quota; a new anonymous session
  cannot refill it. Singapore midnight restores Live while Replay stays usable.
  Separate unit checks cover concurrent admission and persistence across restarts.
- Deployment packaging and frontend build tests confirm that configured secrets
  are absent. No deployment token or OpenAI API access is needed for tests.
  
## Earlier validation — 15 October 2026

- `pytest -q`: **50 passed**, no skips and no warnings. Pytest warnings are errors.
- `ruff check .` and `ruff format --check .`: passed.
- Real Icarus Verilog 13.0 and Verilator 5.052 verify a signed INT8 NPU MAC.
- Integration checks cover successful RTL, functional/syntax repairs, inferred
  latches, fixed testbench contents, saved reports and CLI exit codes.
- Runner checks cover timeouts with partial output, OS errors, log truncation and
  simulation success-marker/exit-status handling.
- OpenAI SDK checks use HTTP-mocked responses for generation, RTL-only repairs,
  refusals and authentication errors. They make no live API calls.

The five CPU/GPU/NPU example specifications are generation inputs. This validation
checks the agent's local verification and repair behavior, not model accuracy or
convergence on those five examples. Historical user run artifacts are preserved.
