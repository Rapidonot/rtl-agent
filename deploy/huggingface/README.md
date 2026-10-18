---
title: RTL Agent Live
emoji: ⚙️
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

Backend for the RTL Agent Vercel website. Configure these in Space Settings,
never in repository files:

Secrets:
- `OPENAI_API_KEY`: your OpenAI project key.

Variables:
- `RTL_AGENT_MODEL`: your actual API model ID.
- `RTL_AGENT_PUBLIC_HOST`: the Space app hostname, e.g. `owner-project.hf.space`.
- `RTL_AGENT_ALLOWED_ORIGINS`: your exact production Vercel origin,
  e.g. `https://project.vercel.app`.
- Optional `RTL_AGENT_HOSTED_MAX_ATTEMPTS=3`,
  `RTL_AGENT_HOSTED_RUN_TIMEOUT=300`, `RTL_AGENT_MAX_OUTPUT_TOKENS=12000`.

Replay is public. Live starts without a password and permits **20 admitted runs
per day across everyone**, resetting at midnight Singapore time (UTC+8). One run
can be active globally. A run includes its repairs; failed and stopped runs count.
Each session sees only its own Live results. The identifier is kept in
sessionStorage and expires after 24 hours. Refreshing or starting another browser
session does not reset the shared daily allowance. Replay remains available.
This counts runs, not dollars; use provider billing controls too.

Free hosting can sleep and its filesystem can reset. Run history and quota
records survive an ordinary process restart on the same disk, but not a disk
reset or rebuild. Export results you want to keep. Export important results before your session ends; there are no user accounts
or cross-device recovery. Avoid submitting sensitive specifications.
