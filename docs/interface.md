# Local web interface

The maintained page is `web/index.html`; root `index.html` redirects to it.
Run `rtl-agent serve` after installing `.[web]`. The UI has no build step,
external font service or browser OpenAI credentials.
See the README for complete macOS setup commands.

For optional Vercel + Docker hosting, see [hosting.md](hosting.md). The frontend
build embeds only `RTL_AGENT_API_BASE_URL`. `serve-hosted` requires an explicit
backend host and exact HTTPS website origins, leaving local guards unchanged.
Visitors need no password: POST `/api/session` issues an opaque random identifier
for run ownership. The backend allows **20 Live runs per day across everyone**,
resetting at midnight Singapore time (UTC+8). `GET /api/health`, session creation
and run admission expose the shared `usage` object: `limit`, `used`, `remaining`,
`day`, `resets_at` (Unix seconds), and `timezone` (`Asia/Singapore`).
The page refreshes this count about once a minute and when the tab becomes visible.
The identifier lives in sessionStorage and expires after 24 hours. Bearer headers
carry that identifier, never the OpenAI key. Cross-site cookies are not needed.
SQLite enforces admission atomically and scopes reports, gallery, waveforms and
cancel requests to the owning session. A run includes all its repairs. Errors
and stops count; busy and invalid requests do not. New sessions do not refill
the daily allowance. Replay and existing results stay available at exhaustion.
Counts and ownership survive process restarts on the same disk; a hosting disk
reset loses them. This is a run limit, not a dollar spending cap.

Replay remains the default and animates the five embedded CPU/GPU/NPU examples.
The candidates are scripted fixtures; their lint, simulation and Yosys evidence
was actually recorded with local tools. Replay makes no API or EDA calls.
Live is enabled only when `/api/health` reports the key, model and tools ready,
with a valid `RTL_AGENT_MAX_OUTPUT_TOKENS` budget (default 12000, range 1024–65536).
Every result identifies its provenance as **Recorded replay**, **Generated live by
<model>**, or **Imported report**.

The existing workbench layout, larger specification panel, condensed waveform,
attempt timings, synthesis explanations and gallery remain. Internal module names
are kept in artifacts and advanced settings, not titles. The header shows only
RTL Agent, and the redundant category tag beneath the main title is removed.
The checks bar is larger,
with mobile cards; desktop uses the available viewport width.

The first visit opens a seven-step guided tour, with highlights on Replay/Live, the
block selector, specification, waveform and attempts, Yosys metrics, checks and
Workbench/Block gallery. It supports Back, Skip, Escape and keyboard navigation;
**Quick tour** reopens it. Completion or skipping is remembered in this browser.
The tour restores the previous page and focus, never edits the specification,
starts a run, selects a different attempt or changes verification evidence.

## Server and engine contract

- `GET /api/health`: key presence, tool availability, model, output token budget, Live readiness/reason
  and worker busy state. No credential values or environment dump.
- `GET /api/examples`: the five specifications from `examples/` with readable titles.
- `GET /api/runs`: metadata for the latest 100 completed local runs with recorded
  attempts, newest first. Invalid reports and paths outside the runs root are
  skipped. No source files or VCD samples are embedded in the listing.
- `POST /api/run {spec, top, max_attempts}`: background worker, immediate run ID;
  custom editable specs accepted; attempt limit clamped to 1–5; 409 if busy.
- `GET /api/run/{id}`: UI schema version 2, real artifacts and metrics, completed
  attempts plus a clearly running/interrupted partial attempt, assumptions, caption
  source/pending state and whether the worker remains active.
- `GET /api/run/{id}/attempts/{n}/waveform`: actual attempt VCD converted to signal
  samples, including failed attempts; `null` when no usable VCD exists. Add
  `?explain=true` for `{waveform, reason}` with the specific display reason.
- `POST /api/run/{id}/cancel`: cooperative cancellation; partial results retained.

`RemoteRunEngine` implements the same `start`, `onEvent` and `cancel` contract as
`ReplayRunEngine`. It polls about once per second, fetches real per-attempt VCDs,
rejects stale responses after a mode/run change, and offers Replay on errors.
A failed waveform request shows a one-line message without changing the run's result.
The browser cannot provide an API key, provider, testbench, output path or model ID.

The gallery defaults to **Your runs**; **Examples** holds the five recorded demos.
Live results enter the gallery automatically when the run finishes. History is
read from local run folders after browser refresh or server restart and requires
no API key. Opening a card fetches the original report and real per-attempt VCDs.
Browser imports and manually chosen names remain supported, with records deduped
by run ID. A gallery display or loading error cannot change verification results.
Generated cards use concise descriptions of the requested block, rather than
truncated model summaries. Their text-only layout avoids suggesting that every
custom block is an ALU. Recorded examples retain their specific illustrations;
titles and footers wrap, and card heights grow to fit the content.

The default local service and `serve` command accept loopback binding only. The app rejects remote
clients, unexpected Host headers and cross-origin requests. Run IDs and resolved
artifact paths are validated against the runs directory, including symlinks.
Access logging is disabled, API responses are not cached, and credential values
are redacted from error responses. EDA commands retain the existing credential-free
environment behavior.

A shared run deadline bounds model requests and EDA command timeouts. Cancellation
is checked between operations; it does not forcibly kill a Python thread. A timed
out or cancelled operation keeps the single-run slot until its worker exits.
Reports are replaced atomically after each completed stage and attempt. An
unfinished run from a previous server process is displayed as interrupted.

## Evidence and captions

The verification order, exact simulation success-marker rule, all-four-stage pass
predicate and latch rejection remain unchanged. The testbench is generated from
the spec in its own model call, validated and hash-locked before RTL generation.
Its bytes stay fixed throughout repairs. Source policy rejects external file/system
tasks and includes in both sources; every testbench dumpfile must be "waves.vcd".
Before locking, a missing testbench timing directive gets `` `timescale 1ns/1ps ``;
existing explicit directives remain byte-for-byte intact.
Stage observers report progress; they cannot establish a pass.
No mutation testing, comparison mode or testbench badge was added.

Waveforms preserve VCD timescale, aliases, wide buses, transitions and unknowns.
The display parser is bounded to 32 MiB, 64 signals and 100,000 changes and labels
subsets partial. Missing metadata or samples are not reconstructed. Failure time
comes from simulator output with explicit units or the VCD timescale. Unknown time
gets no marker; a known failure automatically focuses and zooms its real trace.
The guide below the scope explains wires over time and the failure marker.
Missing or rejected waveforms collapse to a one-line message and a reason tooltip.
Valid finite time ranges are displayed without a fixed nanosecond-duration cap.

The generated assumptions are visible in the specification panel. Each attempt
button says **checks took** and sums tool durations, excluding model generation.
**View artifacts**, **View raw error** and **Inspect Yosys evidence** retain the
original source, locked testbench, RTL diff and exact tool outputs. Generic Yosys
counts and logic depth describe structure, not silicon area or operating speed.

Captions are opt-in on `RTLAgent`; the CLI leaves them disabled and makes no
caption API call or deterministic fallback. The server opts in and invokes
`Provider.caption` once after a passed/failed loop, with the same model and an
explicit whitelist: spec, first failed diagnostics, next change summary, failed
attempt number, total attempts, failure kind, final status, and later-attempt pass.
The prompt requests two short sentences readable by a 14-year-old, bans technical
jargon, separates the earlier failure from the outcome, and treats fact strings
as untrusted data. Failure kind is computed conservatively from recorded evidence;
only known housekeeping warnings are called formatting slips. Invalid sentence
counts and jargon fall back to the deterministic caption.
A deterministic diagnostic caption is published immediately. The model explanation
is optional, asynchronous and capped by a ten-second supervisor; errors, refusals,
invalid responses or late completion retain the fallback. Captions never change
verification fields. **AI-written** appears only for model output.

The run explanation is shown with the first failed attempt, or first passing
attempt if there was no failure. Other selections get their own deterministic
check summary, so a passing waveform is not described as the earlier failure.
The original error stays one click away. Prompt guardrails cannot guarantee model
truthfulness; the raw evidence remains the reference.

## Validation

Pytest uses mocked providers and mocked SDK transports; no OpenAI access is
required. Existing EDA integration tests use the real tools. New tests cover API
endpoints, request validation, one worker at a time, early run IDs, stage snapshots,
path/symlink traversal, localhost/origin guards, key redaction, timeout/cancellation,
caption facts and fallback, and real VCD conversion.

Browser validation runs the full Replay/Live path against a fixture model provider
and real Verilator/Icarus/Yosys, including failure/repair, per-attempt VCDs, the real
fatal timestamp, auto-zoom, raw errors, assumptions, caption attribution, API error
recovery and responsive layouts. Hosted validation also covers cross-origin
automatic sessions, shared daily quotas, real-tool failure/repair and frontend builds
that exclude server secrets. The full suite passes with network calls blocked
in Linux Python 3.12 and in the Python 3.11 Docker container. Live OpenAI generation,
caption quality, macOS Homebrew behavior, account integrations and public cloud
deployment still require verification after the hosting accounts are configured.
