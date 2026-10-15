# RTL Agent

RTL Agent uses compiler and simulation feedback to help an LLM fix small hardware designs. The current focus is building blocks used in CPU, GPU and NPU architectures.

The tool takes a written circuit specification, asks OpenAI for SystemVerilog and a testbench, then runs Verilator and Icarus Verilog locally. If a check fails, it sends the diagnostics back to the model and requests an RTL repair. The testbench stays the same throughout those repairs.

## Examples

- An 8-bit CPU ALU with arithmetic, logic, shifts and status flags.
- An 8×16-bit register file with a hardwired zero register.
- A four-lane GPU SIMD ALU with per-byte saturation.
- A pipelined pixel alpha-blending unit.
- A signed INT8 NPU MAC with a 20-bit accumulator.

The specifications are in `examples/`. These are individual blocks (not complete chips).

## Setup

Requires Python 3.11+, Icarus Verilog and Verilator. On macOS:

```bash
brew install icarus-verilog verilator
git clone https://github.com/Rapidonot/rtl-agent.git
cd rtl-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
rtl-agent doctor
```

Set `OPENAI_API_KEY` in your terminal, then choose a model. I recommend GPT-6 Luna as it is cheap and works well.

```bash
rtl-agent generate --spec-file examples/npu_neural_mac.txt --top npu_neural_mac --model YOUR_MODEL_ID --max-attempts 3
```

Runs save source files, logs and reports under `runs/`; waveforms are saved when produced.

## Verification

A candidate must pass Verilator lint, Icarus compilation and simulation. The testbench must finish its checks and print `RTL_AGENT_PASS` exactly once.

Assertion failures, timeouts and truncated logs count as failures. Repairs can replace only the RTL; the testbench stays unchanged throughout the repair loop.

## Testing without an API

The repo includes mock tests. Run the tests with:

```bash
pytest -q
```

The tests run real EDA tools on an NPU MAC fixture, including designs with deliberate logic, syntax and latch defects. Model responses are simulated in the automated tests, so pytest does not use the API.
