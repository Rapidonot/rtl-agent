The public site has two parts: a static frontend on Vercel and a backend on Render.
Replay is public. Live allows 20 runs per day shared across all visitors, resetting at
midnight Singapore time. The OpenAI key lives only in the backend's secrets, never in
Vercel, GitHub or source files.
