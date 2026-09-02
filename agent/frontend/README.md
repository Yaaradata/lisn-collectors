# LiSN Collector — Ops Console (frontend)

Read-only operational UI for the LiSN diagnostic agent. It answers “was this incident collected?”, “what is missing in this range?”, and open-ended questions over live collector state — without collecting, resetting, or modifying any data.

## What this deliberately cannot do

- **No writes** — no collection triggers, job retries, queue resets, or source mutations.
- **No black-box answers** — diagnose views show the full query chain; chat shows every tool call with expandable queries and results.
- **No SigNoz requirement** — logs/traces are optional; SigNoz unconfigured is grey, not a hard failure.
- **No public backend** — the agent API stays Cloud Run–authenticated. The UI never calls it directly from the browser.

## Two paths — when to use each

| Path | Route | Use when |
|------|-------|----------|
| **Direct diagnosis** | `/diagnose`, `/diagnose/range` | You have an incident id or a time window. Deterministic verdict, stat tiles, window table, gaps. Start here. |
| **Chat** | `/chat` | The question spans systems, needs narration, or you do not know which tool to call. Every reply lists tool calls above the answer. |

The landing page (`/`) shows source health and links to both paths.

## Authentication architecture

The backend (`collector-agent`) requires Cloud Run authentication. Browsers cannot mint Google identity tokens, so **client-side JavaScript must not call the backend URL directly**.

### Option 1 — server proxy (production)

Browser → `/api/agent/*` (Next.js route handlers) → backend with the UI service account token.

- Implemented in `app/api/agent/[...path]/route.ts` and `lib/server/agent-proxy.ts`.
- Cloud Run UI service account (`collector-agent-ui`) holds `roles/run.invoker` on `collector-agent` only.
- No CORS, no token in the browser, backend stays private.

### Option 2 — local development only

```bash
gcloud run services proxy collector-agent --region=asia-south1 --port=8090
export AGENT_BACKEND_URL=http://127.0.0.1:8090
npm run dev
```

Or run the backend locally with the same `AGENT_BACKEND_URL`. **Do not use this pattern in production.**

Under no circumstances make `collector-agent` publicly invokable — it reads Flipkart incident data.

## Run locally

**1. Backend** (port 8090) — local uvicorn or `gcloud run services proxy` as above.

**2. Frontend** (port 3000):

```bash
cd agent/frontend
cp .env.local.example .env.local
npm install
AGENT_BACKEND_URL=http://127.0.0.1:8090 npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The browser calls `/api/agent/*`; the Next.js server forwards to `AGENT_BACKEND_URL`.

## Deploy to Cloud Run

From repo root (requires `.env` with `PROJECT`, `REGION`, and `collector-agent` already deployed):

```bash
make deploy-agent-frontend
# or: bash scripts/deploy_agent_frontend.sh
```

**Build-time vs runtime env (critical):**

| Variable | When | Value |
|----------|------|-------|
| `NEXT_PUBLIC_AGENT_API_URL` | **BUILD** (baked into JS) | Must be `/api/agent` — never `localhost` |
| `AGENT_BACKEND_URL` | **RUNTIME** (server only) | `https://collector-agent-….run.app` |

If you build with `NEXT_PUBLIC_AGENT_API_URL=http://localhost:8090`, production will silently call your laptop. The deploy script and `cloudbuild.yaml` set `/api/agent` explicitly.

Service: `collector-agent-ui` · SA: `collector-agent-ui@…` · min-instances 0 · auth required (never `allUsers`).

## Project layout

Next.js App Router. **`components/` and `lib/` are siblings of `app/`, not inside it.**

```
agent/frontend/
├── app/
│   ├── api/agent/[...path]/   # authenticated backend proxy
│   ├── diagnose/              # direct diagnosis routes
│   └── chat/
├── components/
├── lib/
│   ├── server/agent-proxy.ts  # ID token + forward
│   └── …
├── Dockerfile                 # standalone multi-stage
└── cloudbuild.yaml
```

## Build (standalone)

```bash
docker build \
  --build-arg NEXT_PUBLIC_AGENT_API_URL=/api/agent \
  -t collector-agent-ui .
npm run build   # local; set NEXT_PUBLIC_AGENT_API_URL=/api/agent for parity
```

Output mode: `standalone` in `next.config.ts` — small runtime image, `node server.js` on port 3000.
