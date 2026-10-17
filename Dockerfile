# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    RTL_AGENT_RUNS_DIR=/home/app/data/runs

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates build-essential iverilog verilator yosys \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web
COPY examples ./examples
# The optional CA is for managed build environments, never an application secret.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      PIP_CERT=/run/secrets/proxy_ca python -m pip install --no-cache-dir '.[web]'; \
    else python -m pip install --no-cache-dir '.[web]'; fi

RUN mkdir -p /home/app/data/runs && chown -R app:app /home/app/data
USER app
EXPOSE 7860
CMD ["rtl-agent", "serve-hosted", "--port", "7860"]
