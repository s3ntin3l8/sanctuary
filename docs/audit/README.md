# Audit reports

Every audit in this repo is one report in this folder. The report is committed; the
evidence is local. Structure, naming and rules: the `audit` skill
([`s3ntin3l8/ai-agent-skills`](https://github.com/s3ntin3l8/ai-agent-skills), `plugins/audit`).

## This repo

- **Scope token:** `frontend` (the React SPA in `frontend/`).
- **Capture:** from the repo root, with Docker (no Node or Playwright setup: the browser is in the pinned
  [visual-kit](https://github.com/s3ntin3l8/visual-kit) image):

  ```bash
  make screenshots-audit AUDIT=<slug>        # every scene tagged "audit" in visual/scenes.json
  make screenshots-audit-auth AUDIT=<slug>   # /login and /signup (the demo with the login screen on)
  ```

  It starts an isolated demo (`make demo`, see below) with a fixed clock and renders each scene on
  desktop, tablet and phone in dark and light, with axe, console errors, horizontal overflow and
  unnamed controls recorded in `metrics.json`. Captures go to `docs/audit/assets/<slug>/` (and
  `<slug>-auth/`), which is gitignored. Record the visual-kit version from `visual/Dockerfile` in the
  report's Evidence section, so the capture can be reproduced after the image moves.

- **The demo (`make demo`):** a throwaway Sanctuary with seeded German sample data
  (`scripts/seed_dummy_data.py`) on `127.0.0.1:3985`, auth off, no Redis, no AI backend.
  It runs from a scratch copy of the code against its own pgvector Postgres container
  (`127.0.0.1:3986`), so it never reads the repo `.env` or touches `data/` or the dev
  database, even while the dev stack runs. It needs Docker and a built SPA (it builds
  `frontend/dist` when missing or stale). `Ctrl-C` or `SIGTERM` removes everything;
  `make demo KEEP=1` keeps the scratch directory. The guard is `scripts/demo/env.py`.
  Ports are overridable with `DEMO_PORT` / `DEMO_DB_PORT` (keep `demo.url` in `visual/scenes.json` in step).
  `DEMO_NOW=<ISO date-time>` fixes the clock of the seed and the server (the make targets set it from
  `visual/scenes.json`), so deadlines and "x days late" read the same on any day.

- **Themes:** both are captured (file names end in `.dark` / `.light`). The
  app's theme is System by default (Settings → Appearance), so the emulated OS setting picks it;
  a saved Dark/Light choice would win, and the demo browser has none.

- **Capture limits:**
  - `/document/<id>` uses numeric ids (30 Klage, 36 Klageerwiderung, 47 Einkommensgutachten, 68 Anlagenkonvolut for
    `/document/68/slice`) and `/ingest/slice/11` the scan batch awaiting cuts (both PDFs are written by
    the seed into the demo's scratch `data/`).
    They are stable only because the demo database is fresh every run; reseed order matters.
  - Auth pass: `/login` and `/signup` need the login screen on, hence the separate `screenshots-audit-auth`
    target ([`visual/scenes.auth.json`](../../visual/scenes.auth.json)). These are the seeded screens (the
    seed always creates `admin@localhost`, so the app is not in first-run state): the first-run create-admin
    pair is not covered.
  - Not captured: `/oauth/authorize` (needs OAuth query params). Anything that needs clicking through (dialogs,
    menus) is a scene with `steps`, e.g. `triage-filter`.

## Index

| Audit | Scope | Outcome |
| ----- | ----- | ------- |
| [2026-10-frontend-ui-ux](2026-10-frontend-ui-ux.md) | frontend | 5 P1 contrast fixes + icon fix; 9 issues deferred (#451–#459) |
