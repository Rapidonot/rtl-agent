# RTL Agent

RTL Agent uses compiler and simulation feedback to help an LLM fix small hardware designs. The current focus is building blocks used in CPU, GPU and NPU architectures.

The tool takes a written circuit specification, asks OpenAI for a testbench and SystemVerilog, then runs Verilator, Icarus Verilog and Yosys locally. If a check fails, it sends the diagnostics back to the model and requests an RTL repair. The testbench is generated in a separate call before the RTL and stays the same throughout those repairs.

## Website

You can try the [web version here](https://rtl-agent-seven.vercel.app).

**Live** lets you enter a circuit specification and see the generated design, verification results, waveforms and synthesis metrics without installing the tools locally. No API key is needed from visitors. There are 20 Live runs available per day across everyone, resetting at midnight Singapore time.

**Replay** lets you inspect recorded CPU, GPU and NPU examples without using the daily allowance. These examples use scripted RTL candidates checked with actual EDA tools.

The frontend runs on Vercel, and the backend runs on Render.

## Examples used in replay

- An 8-bit CPU ALU with arithmetic, logic, shifts and status flags.
- An 8×16-bit register file with a hardwired zero register.
- A four-lane GPU SIMD ALU with per-byte saturation.
- A pipelined pixel alpha-blending unit.
- A signed INT8 NPU MAC with a 20-bit accumulator.

The specifications are in `examples/`. These are individual blocks (not complete chips).

## Local setup

Requires Python 3.11+, Icarus Verilog, Verilator and Yosys. On macOS:

```bash
brew install icarus-verilog verilator yosys
git clone https://github.com/Rapidonot/rtl-agent.git
cd rtl-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,web]'
rtl-agent doctor
```

Set `OPENAI_API_KEY` in your terminal, then choose a model. I recommend GPT-6 Luna as it is cheap and works well.

```bash
rtl-agent generate --spec-file examples/npu_neural_mac.txt --top npu_neural_mac --model YOUR_MODEL_ID --max-attempts 3
```

Runs save source files, logs and reports under `runs/`; waveforms are saved when produced.

To run the website locally:

```bash
rtl-agent serve
```

Open `http://127.0.0.1:8000`. For local Live generation, also set `RTL_AGENT_MODEL` to your chosen API model ID in the server terminal.

## Verification

A candidate must pass Verilator lint, Icarus compilation and simulation, and Yosys synthesis. The testbench must finish its checks and print `RTL_AGENT_PASS` exactly once.

Assertion failures, timeouts, truncated logs and inferred latches count as failures. Repairs can replace only the RTL; the testbench stays unchanged throughout the repair loop.

Yosys reports gate counts, flip-flop counts and logic depth. When a simulation fails and a waveform is available, the final 12 VCD timestamps are included in the repair prompt.

## Testing without an API

The repo includes mock tests ( examples used above )  Run the tests with:

```bash
pytest -q
```

The tests run  EDA tools on an NPU MAC fixture, including designs with deliberate logic, syntax and latch defects. They also check the web API, session ownership and daily run limit. Model responses are simulated in the automated tests, so pytest does not use the API.
