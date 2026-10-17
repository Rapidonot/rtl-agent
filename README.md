# RTL Agent

Describe a chip block in words. An AI writes the hardware design, and real local
tools check it. The website has two modes:

- **Replay** (the default): inspect and replay the bundled CPU, GPU and NPU results.
  These use scripted RTL candidates and real saved tool results. No API key or
  installed EDA tools are needed for Replay.
- **Live**: submit an editable specification to OpenAI, then run Verilator, Icarus
  and Yosys locally or on the hosted backend. The browser never receives your API key.

Local development remains the default. An optional hosted mode connects a Vercel
frontend to a Docker backend, with a separate Live access password and run limits.
See [the hosting guide](docs/hosting.md) for the Vercel + Hugging Face setup.
Vercel's Git integration and the included, opt-in backend workflow can deploy
updates from GitHub's `web-ui` branch. GitHub needs only a scoped Space deployment
token; the OpenAI key and visitor access password stay in Hugging Face Secrets.
The OpenAI key belongs only in backend secrets; the frontend build reads only a
public backend URL. `rtl-agent serve` remains localhost-only.

## Run the website on a Mac

From your checkout, in Terminal:

```sh
brew install python@3.11 icarus-verilog verilator yosys
cd /path/to/rtl-agent
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[web]'
rtl-agent doctor
rtl-agent serve
```

Open **http://127.0.0.1:8000**. Replay works immediately. Live stays disabled until
all four binaries (`iverilog`, `vvp`, `verilator`, `yosys`), the key and model are
available to the server.

A short guided tour opens on your first visit. Its highlights walk through Replay and
Live, choosing a block, writing a specification, reading waveforms, Yosys results,
checks and the gallery. Use **Skip** or Escape to leave, or **Quick tour** in the
header to open it again. The tour does not start a run or change your results.

For Live, stop the server with Ctrl-C and set these in the same terminal. Use an
actual model ID available to your OpenAI account; no model name is guessed.
In macOS's default zsh, this reads the key without putting it in the command:

```zsh
read -s 'OPENAI_API_KEY?OpenAI API key: '
printf '\n'
export OPENAI_API_KEY
export RTL_AGENT_MODEL='YOUR_MODEL_ID'
export RTL_AGENT_MAX_OUTPUT_TOKENS=12000
rtl-agent doctor
rtl-agent serve
```

`RTL_AGENT_MAX_OUTPUT_TOKENS` sets the web output budget for each testbench, RTL,
and repair request: default **12000**, valid range **1024–65536**.
`rtl-agent doctor` displays the budget. Invalid values disable Live with an
explanation. The CLI `generate` command uses `--max-output-tokens` instead.

Choose **Live**, select an example or edit the specification, set the top module
under Run settings, and click **Run design**. The model field displays the server's
`RTL_AGENT_MODEL`; the browser cannot choose a different model or submit a key.
Assumptions are displayed beside the specification. Source, the fixed testbench,
RTL diffs and original logs remain in **View artifacts**.

The **Block gallery** opens on **Your runs**. Completed Live results, including
failed runs with recorded attempts, appear automatically from the local `runs/`
folder. They remain available after refreshing the browser or restarting the
server, even without an API key. Open a card to reload its real source, logs,
metrics and waveforms. The gallery lists the latest 100 local runs; it does not
delete older run folders. The bundled demos are under **Examples**. Imported
reports and optional names saved with **Save result** remain in this browser.
Generated cards use complete, plain-language descriptions of the requested block
and a text-only layout. The recorded examples keep their specific illustrations.

The server binds only to a loopback address. For a different local port or run
budget:

```sh
rtl-agent serve --port 8001 --run-timeout 600
```

Web runs allow 1–5 attempts and one active verification worker at a time. The
wall-clock budget defaults to 600 seconds. Model requests and EDA commands use
the remaining budget; timeout or Stop prevents further stages or repairs.
Cancellation is cooperative: an in-flight operation must return or reach its
request/command timeout before its thread exits. The worker slot remains occupied
until then, even when the UI already reports the stop. Partial artifacts are kept.
Restarting the server does not resume unfinished runs.

## Examples

The five specifications in `examples/` describe individual blocks, not full chips:

- An 8-bit CPU ALU with arithmetic, logic, shifts and status flags.
- An 8×16-bit register file with a hardwired zero register.
- A four-lane GPU SIMD ALU with per-byte saturation.
- A pipelined pixel alpha-blending unit.
- A signed INT8 NPU MAC with a 20-bit accumulator.

## Verification and evidence

Verification behavior is unchanged: a candidate must pass Verilator lint, Icarus
compilation and simulation, and latch-free Yosys synthesis. Simulation must exit
successfully and print exactly one standalone `RTL_AGENT_PASS` line. Assertion
failures, timeouts, truncated logs and inferred latches cannot establish a pass.
Passing these checks is not a formal correctness proof.

Unless a testbench is supplied with `--tb-file`, generation starts with a separate
model call that receives only the specification and top module name, never RTL.
That call returns a self-checking testbench and its assumptions. The testbench is
validated, saved and hash-locked **before** a second call generates RTL against it.
Assumptions from both calls are merged in order, without duplicates. A supplied
testbench skips the first call and is validated and locked before RTL generation.
If a testbench has no timing directive, the agent prepends `` `timescale 1ns/1ps ``
before saving its hash. Existing explicit timing is preserved; repairs use exactly
the normalized, locked bytes.
Repairs replace only RTL; the same testbench bytes are used throughout the run.

Runs save source, logs, reports and any VCD under `runs/`. `report.json` is replaced
atomically at initialization, after every completed tool stage, after each attempt,
and at completion. Pending stage snapshots are never marked as passed attempts.

The scope uses each attempt's actual `waves.vcd`, including failed simulations.
It preserves recorded unknown values and timescale, and does not manufacture missing
samples. The failure marker uses the simulator's explicit fatal timestamp and the
VCD timescale. Unknown time means no marker. Large traces are bounded for display:
32 MiB input, 64 signals, 100,000 value changes; any displayed subset is marked
partial. Full local artifacts remain on disk. Check times sum tool durations and
exclude model generation time. Yosys counts are circuit structure, not physical
chip area or measured operating speed.
An unavailable trace is replaced by a one-line message with its actual reason in
a tooltip. Recorded traces keep their real time range, including seconds expressed
in nanoseconds; a display problem never changes a verification result.

After a passed or failed **web** run, the same model is asked once for two
plain-English sentences. It receives **only** the spec, first failed attempt's
diagnostics, the next attempt's change summary, the failed attempt number, total
attempt count, failure category, final status, and whether a later attempt passed.
The two-sentence explanation uses everyday words, separates the earlier failure
from the final outcome, and calls formatting-only warnings a formatting slip.
A deterministic caption is saved immediately; an asynchronous model caption may replace it within
10 seconds. Failure or timeout keeps the fallback and never changes pass/fail.
Model prose is labeled **AI-written**; **View raw error** retains the original
reference. The explanation is shown with the first failed attempt (or first passing
attempt when no failure exists); selecting another attempt shows its own check
summary. Captions cannot prove their own accuracy. Captions are opt-in on
`RTLAgent` (`captions=False` by default); the server enables them. CLI `generate`
makes no caption API call, saves no fallback caption, and leaves `caption_pending`
false in `report.json`.

## CLI workflows

The existing workflows remain available:

```sh
rtl-agent generate --spec-file examples/npu_neural_mac.txt --top npu_neural_mac --max-attempts 3
rtl-agent inspect runs/YOUR_RUN_ID
```

`generate` uses `RTL_AGENT_MODEL`, or accepts `--model YOUR_MODEL_ID`. A supplied
`--tb-file` remains supported by the CLI. The website uses the existing generated
testbench flow and does not accept a replacement testbench.

## Test without API access

```sh
python -m pip install -e '.[dev,web]'
pytest -q
ruff check src tests
```

Providers are mocked, including SDK HTTP transport tests. EDA integration tests use
local fixtures and the real tools; they skip if the tools are missing. Server guard
tests use a mock provider and toolchain and need no OpenAI connection. Without the
optional web extra, web tests skip and the CLI remains usable.

The frontend is `web/index.html`; root `index.html` redirects to it. Local use
requires no frontend build step or external asset service. Hosted Vercel builds
use `scripts/build-web.mjs` to add the public backend URL. Wheels include the page
and the five example specs.
