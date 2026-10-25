# Audit reports

Every audit in this repo is one report in this folder. The report is committed; the
evidence is local. Structure, naming and rules: the `audit` skill
([`s3ntin3l8/ai-agent-skills`](https://github.com/s3ntin3l8/ai-agent-skills), `plugins/audit`).

## This repo

- **Scope token:** `frontend` (the React SPA in `frontend/`).
- **Capture:** from the repo root, with a checkout of
  [`ai-agent-skills`](https://github.com/s3ntin3l8/ai-agent-skills) (the `audit` plugin):

  ```bash
  npm --prefix frontend ci && npx --prefix frontend playwright install chromium   # once
  node <path to ai-agent-skills>/plugins/audit/skills/audit/scripts/capture.mjs <slug>
  ```

  It starts an isolated demo (`make demo`, see below) and reads [`capture.json`](capture.json),
  the repo's usual config; an audit whose routes differ adds its own `capture.<slug>.json`.
  Captures go to `docs/audit/assets/<slug>/`, which is gitignored. Record the
  `ai-agent-skills` commit you ran in the report's Evidence section, so the capture can be
  reproduced after the script changes.

- **The demo (`make demo`):** a throwaway Sanctuary with seeded German sample data
  (`scripts/seed_dummy_data.py`) on `127.0.0.1:3985`, auth off, no Redis, no AI backend.
  It runs from a scratch copy of the code against its own pgvector Postgres container
  (`127.0.0.1:3986`), so it never reads the repo `.env` or touches `data/` or the dev
  database, even while the dev stack runs. It needs Docker and a built SPA (it builds
  `frontend/dist` when missing or stale). `Ctrl-C` or `SIGTERM` removes everything;
  `make demo KEEP=1` keeps the scratch directory. The guard is `scripts/demo/env.py`.
  Ports are overridable with `DEMO_PORT` / `DEMO_DB_PORT` (keep `baseUrl` in step).

- **Capture limits:**
  - `/document/<id>` uses numeric ids (30 Klage, 36 Klageerwiderung, 47 Einkommensgutachten).
    They are stable only because the demo database is fresh every run; reseed order matters.
  - Auth pass: `/login` and `/signup` need the login screen on. Run a second pass with
    `node <path to ai-agent-skills>/plugins/audit/skills/audit/scripts/capture.mjs <slug> --config docs/audit/capture.auth.json`
    (it starts `DEMO_AUTH=1 make demo`; same `assets/<slug>/` folder, different file names). These are the
    seeded screens (the seed always creates `admin@localhost`, so the app is not in first-run state):
    the first-run create-admin pair is not covered.
  - Not captured: the slice views (`/ingest/slice/:batchId`, `/document/:docId/slice`; the
    seed has no files on disk, [#445](https://github.com/s3ntin3l8/sanctuary/issues/445)), `/oauth/authorize`,
    and anything that needs clicking through (dialogs, modals).

## Index

| Audit | Scope | Outcome |
| ----- | ----- | ------- |
