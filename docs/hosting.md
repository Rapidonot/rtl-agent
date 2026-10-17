# Vercel website + Hugging Face Live backend

Replay is public. Invited visitors enter a separate access password for Live;
the OpenAI key remains in backend secrets. Hosting can fit free tiers, but model
requests are billed to your OpenAI account. GitHub can store the code and trigger
both deployments. The OpenAI key lives only in Hugging Face Secrets, not GitHub
or Vercel. A manual deployment path is also available below.

The local `rtl-agent serve` workflow stays unchanged. Deployment uses the new
explicit `rtl-agent serve-hosted` command. Verification rules, the frozen
testbench and the exact pass-marker requirement are the same in both modes.

## Recommended: GitHub with automatic deployments

1. Create your [Vercel](https://vercel.com/signup) and
   [Hugging Face](https://huggingface.co/join) accounts. In Hugging Face, create a
   **Public Docker Space**, blank template, free CPU hardware when offered.
   Copy its app URL ending in `.hf.space`; the source code will be public.
2. Put the updated project files on your GitHub **web-ui** branch. Keep `.env`,
   `.venv`, `.vercel` and `runs/` out of Git; the included ignore files do this.
   Review files before committing. No actual credentials belong in source files.
3. In Vercel, choose **Add New → Project → Import Git Repository** and select
   `Rapidonot/rtl-agent`. Use the repository root, **Other** framework, the included
   `node scripts/build-web.mjs` command and `dist` output. Add the environment
   variable `RTL_AGENT_API_BASE_URL` with your Space app URL. No API key or access
   password goes in Vercel. Select **web-ui** as the Production Branch in the
   project's Git settings; redeploy that branch if the initial import used main.
4. Copy Vercel's production origin (such as `https://my-rtl-agent.vercel.app`).
   Add the Hugging Face secrets and variables in the configuration table below.
   In particular, set its allowed origin to this exact Vercel origin.
5. For automatic backend updates, create a **fine-grained Hugging Face token**
   with write access only to this Space. In GitHub repository **Settings → Secrets
   and variables → Actions**, add secret **HF_TOKEN** with that token. In the
   **Variables** tab add **HF_SPACE_ID**, such as `yourname/rtl-agent-live`.
   GitHub needs this deployment token only. Keep the OpenAI key and the visitor
   access password exclusively in Hugging Face Secrets.
6. Open GitHub **Actions → Deploy Live backend → Run workflow**, choose web-ui,
   and run it for the first upload. If the workflow is not yet available in the
   Actions UI, a push that adds or changes its file on web-ui also triggers it.
   Watch the Space build until it is Running, then test Live on the Vercel site.

Subsequent pushes to **web-ui** deploy the frontend through Vercel's Git
integration. Changes affecting the backend also upload the allowlisted backend
files through the included GitHub Actions workflow. Its upload replaces the
Space's source files from this repository; Space Secrets stay in its settings.
The workflow never runs for pull requests and stays inactive until HF_SPACE_ID
is configured. To use a different production branch, change both Vercel's
Production Branch and `.github/workflows/deploy-backend.yml`'s branch filter.

GitHub Actions reports the upload; Hugging Face reports the subsequent Docker
build. Check both for a failed backend deployment. Vercel preview URLs require
their own exact allowed-origin entries if they should have Live access.

The remaining sections give the full configuration and an alternative manual
upload route. With Git integration, skip the manual CLI and file-upload steps.

## Manual alternative: 1. Create the accounts and the Space

Create accounts at [Vercel](https://vercel.com/signup) and
[Hugging Face](https://huggingface.co/join), if needed.
In Hugging Face, create a new **Space**, select **Docker**, the blank template,
**Public** visibility and the free CPU hardware tier, when offered.
The Space source code will be public. Credentials will not be in its files.

Copy the Space's **app URL**, which ends in `.hf.space`. Get it from Open App /
Embed this Space; it is different from the `huggingface.co/spaces/...` page URL.
You will use, for example, `https://owner-rtl-agent-live.hf.space`.
No working app is expected until you upload the backend and configure it below.

## 2. Create your Vercel project without GitHub

Use the **frontend deployment ZIP**, not the old static showcase ZIP. Unzip it.
The folder contains `vercel.json`, `scripts/build-web.mjs` and `web/index.html`.

On your Mac, install Node if it is not already installed:

```sh
brew install node
```

In Terminal, open the extracted frontend folder and run:

```sh
cd /path/to/rtl-agent-vercel
npx vercel@latest login
npx vercel@latest link
```

Choose your personal account and create a new project. Use **Other** for the
framework if asked. Keep the included build command and `dist` output directory.
Do not connect a Git repository. Copy your exact production website origin from
the project dashboard, for example `https://my-rtl-agent.vercel.app`.

Add this one Vercel environment variable for Production:

```sh
npx vercel@latest env add RTL_AGENT_API_BASE_URL production
```

At its prompt, enter the Space app URL, such as
`https://owner-rtl-agent-live.hf.space`. Use the origin only: no `/api`, path,
credentials, query string or fragment. This URL is intentionally public.
Do not add `OPENAI_API_KEY` or the Live password to Vercel.

## 3. Configure the backend in Space Settings

In the Space's **Settings → Variables and secrets**, add:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `OPENAI_API_KEY` | Your OpenAI project key |
| Secret | `RTL_AGENT_LIVE_PASSWORD` | A random password, 24–256 non-space ASCII characters |
| Variable | `RTL_AGENT_MODEL` | Your actual available OpenAI API model ID |
| Variable | `RTL_AGENT_PUBLIC_HOST` | The app hostname, e.g. `owner-rtl-agent-live.hf.space` |
| Variable | `RTL_AGENT_ALLOWED_ORIGINS` | The exact production Vercel origin, e.g. `https://my-rtl-agent.vercel.app` |
| Variable | `RTL_AGENT_DAILY_RUN_LIMIT` | `10` initially |
| Variable | `RTL_AGENT_HOSTED_MAX_ATTEMPTS` | `3` initially |
| Variable | `RTL_AGENT_HOSTED_RUN_TIMEOUT` | `300` seconds initially |
| Variable | `RTL_AGENT_MAX_OUTPUT_TOKENS` | `12000` initially |

Generate the access password with a password manager or locally with:

```sh
openssl rand -hex 24
```

Save it in a password manager and share it privately with invited visitors.
It is separate from your OpenAI key. Never paste either secret into source files,
Vercel frontend settings, Git, a website URL or chat.

Origins must be exact HTTPS origins, with no paths or wildcards. For multiple
sites, separate them with commas. Preview deployments and custom domains each
need their own explicit entry; allowing one Vercel domain does not allow all of
Vercel. The backend's own HTTPS app origin is allowed automatically.

## 4. Upload and start the backend

Unzip the **backend deployment ZIP**. In the Space's **Files** tab, use **Add file
→ Upload files** and upload the contents, preserving the folders:

```text
Dockerfile
.dockerignore
README.md
pyproject.toml
src/rtl_agent/...
web/index.html
examples/...
```

The Dockerfile and README must be at the Space root, not inside another folder.
Use the README supplied in the backend ZIP: its front matter selects Docker and
port 7860. The image installs Python 3.11, Verilator, Icarus and Yosys, runs as a
non-root user and starts one backend process. No key is baked into the image.

Wait for the Space to show **Running**. Missing settings produce a clear startup
error. At `https://YOUR-SPACE.hf.space/api/health`, check that `live_available` is
true and `authentication_required` is true. This public health response reports
key presence, never the key. The Live APIs remain locked.

## 5. Deploy the website

Back in the extracted frontend folder, run:

```sh
npx vercel@latest --prod
```

Open the production URL returned by Vercel. If it differs from the origin you
configured, update `RTL_AGENT_ALLOWED_ORIGINS` in the Space settings to match it.
Replay works without a password. Click **Live**, enter the access password, then
submit a specification. **Lock Live** removes access from this tab. Refreshing
the page also requires the password again. The app never saves it in localStorage
or sessionStorage. Cross-site cookies are not required.

The backend's key is used only for server-side model requests. All browser API
requests go to the configured backend; authorization uses the access password,
never the OpenAI key. The Vercel build reads only `RTL_AGENT_API_BASE_URL` and
copies only the frontend into `dist/`.

## Limits, storage and updates

- One active Live run is allowed globally. Other visitors receive a busy message.
  Deploy exactly one backend replica and worker; the worker lock is per process.
- The default shared allowance is 10 admitted runs per UTC day, counting errors,
  failures and stops because they may incur model costs. Busy and unauthorized
  requests do not consume the allowance. The SQLite counter survives a process
  restart on the same disk. This is a run quota, not a dollar spending cap.
- Everyone with the shared password can see and stop the shared Live runs. This
  is an invited demo, not a service with separate user accounts or private galleries.
- Free hosting may sleep and may reset its disk when rebuilt or restarted on a
  fresh machine. History and quota records disappear if that disk is lost.
  Export important results. Paid persistent storage is optional.
- Token budgets default to 12000 per testbench, RTL or repair request. Several
  requests can occur in one run; the optional final caption adds a short request.
  Use the billing alerts and spending controls available in your OpenAI account.
- If the Space is waking up, wait and use **Retry connection**. A disabled Live
  button can also indicate missing tools, model, key or an invalid output budget.
  An origin error usually means the allowed website URL does not match exactly.
- Update the frontend with another `npx vercel@latest --prod` from the updated
  frontend folder. Backend file uploads rebuild the Space. Keys stay in its
  settings. Rotate the access password there to revoke previous access.

## Local verification before publishing

```sh
python -m pip install -e '.[dev,web]'
pytest -q
ruff check src tests
RTL_AGENT_API_BASE_URL=https://YOUR-SPACE.hf.space node scripts/build-web.mjs
```

For a local Docker build, `docker build -t rtl-agent-hosted .` builds the backend.
Run it with your hosting settings supplied through your container platform's
runtime secrets. Do not pass secrets as Docker build arguments or copy an env
file into the image. Vercel, Hugging Face account configuration, actual cloud
domain routing and live OpenAI model behavior still need verification after you
create and configure those accounts.
