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
- `RTL_AGENT_LIVE_PASSWORD`: a random 24–256 character ASCII password shared
  only with invited visitors. Generate one with `openssl rand -hex 24`.

Variables:
- `RTL_AGENT_MODEL`: your actual API model ID.
- `RTL_AGENT_PUBLIC_HOST`: the Space app hostname, e.g. `owner-project.hf.space`.
- `RTL_AGENT_ALLOWED_ORIGINS`: your exact production Vercel origin,
  e.g. `https://project.vercel.app`.
- Optional `RTL_AGENT_DAILY_RUN_LIMIT=10`, `RTL_AGENT_HOSTED_MAX_ATTEMPTS=3`,
  `RTL_AGENT_HOSTED_RUN_TIMEOUT=300`, `RTL_AGENT_MAX_OUTPUT_TOKENS=12000`.

Replay is public. Live APIs and the shared Live run gallery require the access
password. One backend process, one active run. The quota counts admitted runs,
including failed and stopped runs, per UTC day; it is not a dollar spending cap.
Use provider billing controls too.

Free hosting can sleep and its filesystem can reset. Run history and quota
records survive an ordinary process restart on the same disk, but not a disk
reset or rebuild. Export results you want to keep. Do not treat the shared gallery
as private storage or submit sensitive specifications.
