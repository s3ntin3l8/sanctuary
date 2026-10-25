# UI/UX audit — frontend

**Scope.** The React SPA in `frontend/`: every route in `docs/audit/capture.json` (28 routes, plus `/login` and `/signup`) at phone (390×844) and tablet (820×1180), in the dark and the light theme. Out of scope: desktop-width layout, interactions that need clicking through (modals, drawers, dialogs), `/oauth/authorize`, and real (non-demo) data volumes.

**Status.** The contrast failures (P1) and one cross-cutting icon defect are fixed in this PR. Everything else is in the [Deferred backlog](#deferred-backlog).

---

## How the audit was run

| Layer | Method |
|---|---|
| Captures | `make demo` (isolated, seeded) driven by the shared `capture.mjs`: 112 screenshots in the main pass, 8 in the auth pass. Dark and light because the app's theme is now System, so the emulated OS setting picks it. |
| axe | `@axe-core/playwright` on every main-pass page (112 scans), before and after the fixes. |
| Visual review | Three reviewers split by route group read the screenshots. Every finding recorded here was re-checked by me against the PNG and, where it was a claim about overflow or hidden controls, measured in the running demo (`scrollWidth`, bounding boxes and the overflow of each ancestor at 390px). Unverified reviewer remarks are not in this report. |
| Code | Contrast computed from the real tokens in `frontend/src/styles/index.css` for both themes against all six surfaces; accessible names on the rail and slicing controls; dialog focus handling (`ui/useOverlay.ts`); focus styles; landmarks; animation and `prefers-reduced-motion`. |

### What the code already does well

Checked and left alone. A fix here would be a regression.

- **No page scrolls sideways.** At 390px `documentElement.scrollWidth` is 390 on every measured route; wide content (cases table, costs ledger, triage grid, slice header) lives in its own horizontal scroller, so the page itself never breaks.
- **Dialogs trap and restore focus.** `ui/useOverlay.ts` handles Escape (topmost overlay only), Tab containment and focus restore; palette, `Modal`, `Drawer` and the page viewer use `role="dialog"` + `aria-modal`.
- **Visible focus** everywhere: one global `:focus-visible` rule in `index.css`.
- **Icon-only controls have names.** The rail, the theme toggle and the split buttons carry `aria-label`s (the open issue is about their *content*, not their absence).
- **Theme parity.** Layout is identical in both themes; the light theme's borders, badges and highlights stay legible; `login`/`signup` are clean at every size.
- **Text tokens.** `ink`, `ink2`, `muted`, `tealink`, `accent`, `danger`, `info` already met 4.5:1 on every surface in both themes.

---

## Severity model

- **P0**: a supported viewport or theme renders a core surface (cases, triage, document HUD) unusable, or shows wrong data. None found.
- **P1**: a WCAG 2.2 AA failure on shipped UI, or a defect that blocks a task with no workaround.
- **P2**: a real defect with a workaround (a control reachable only by sideways scroll, an announcement missing, a cramped phone layout).
- **P3**: polish.

---

## Findings

| # | Sev | Finding | Evidence | Where | What changed |
|---|---|---|---|---|---|
| 1 | P1 | Light theme: `--warning` (3.6–4.0:1) and `--success` (3.1–3.4:1) fail AA as text on every surface; used for 10px badges, due-date headings, "no case assigned" | axe `color-contrast`: 58 pages, 83 nodes; token calculation | `styles/index.css` | Light `--warning` `#a1791a`→`#7a5800`, `--success` `#3f9b6e`→`#216640` (≥4.6:1 on every surface and on the tinted fills). axe: 0 nodes |
| 2 | P1 | `--muted2` fails AA in both themes (3.0–3.5 dark, 2.8–3.1 light); used for placeholders, "No open action", timestamps | Token calculation (25 uses) | `styles/index.css` | Dark `#5f6d86`→`#7d8ca6`, light `#8a9599`→`#626d70` (4.5–4.8:1) |
| 3 | P1 | Danger button: white text on `#f87171` in dark is 2.77:1; "Create & assign" is white on the teal accent (1.4:1 in dark) | axe; token calculation | `ui/Button.tsx`, `features/triage/CasePicker.tsx` | New `--on-danger`; both buttons use the theme's on-colour (`text-on-danger`, `text-on-accent`) |
| 4 | P1 | Rail count badge: `#3b2a00` on the warning fill is 3.47:1 at 9px; and would worsen with a darker light warning | axe: 48 nodes | `shell/RailButton.tsx` | New `--on-warning` token (dark text on the dark theme's amber, white on the light theme's brown) |
| 5 | P1 | Link in running text distinguished by colour only (2.03:1 against the surrounding text) | axe `link-in-text-block` | `features/admin/AdminAgentAccessPage.tsx` | The link is underlined |
| 6 | P2 | Ligature icons shrink to a speck in tight flex rows on phone: the Appearance nav item has no icon, KPI tile icons on Home render as stray dots, the Identity icon is tiny | Phone screenshots `settings-*`, `home` | `.icon` utility in `styles/index.css` | `flex-shrink: 0` on `.icon`; verified in a phone capture |
| 7 | P2 | Rail badge counts are not in the accessible names | axe `label-content-name-mismatch`: 112 pages | `shell/RailButton.tsx` | Deferred: [#451](https://github.com/s3ntin3l8/sanctuary/issues/451) |
| 8 | P2 | Triage rows and graph relay nodes nest focusable controls in `role=button` | axe `nested-interactive`: 48 nodes | `features/triage/BundleTree.tsx`, `GraphTab.tsx` | Deferred: [#452](https://github.com/s3ntin3l8/sanctuary/issues/452) |
| 9 | P2 | Second `<main>` on case pages and the 404 page; `/search` has no h1 | axe landmark rules: 16 pages; `page-has-heading-one` | `CasePage.tsx`, `NotFoundPage.tsx`, `SearchPage.tsx` | Deferred: [#453](https://github.com/s3ntin3l8/sanctuary/issues/453) |
| 10 | P2 | Phone: horizontal scrollers have no hint, so `Confirm ↵` in the slicing review, the costs amounts, the cases and triage columns look cut off; the slice cut bar is squashed | Screenshots `document-68-slice`, `ingest-slice-11`, `costs`, `cases`, `triage` (phone); measured in the demo | slice views, ledger, tables | Deferred: [#454](https://github.com/s3ntin3l8/sanctuary/issues/454) |
| 11 | P2 | Phone: case page header takes 620 of 844px; graph toolbar and legend wrap and collide; only the first lane is visible | `cases-ADV-024-A.phone.*` | `CasePage.tsx`, `GraphTab.tsx` | Deferred: [#455](https://github.com/s3ntin3l8/sanctuary/issues/455) |
| 12 | P2 | Phone: settings sub-nav takes ~40% of the first screen; admin Create-user form, Data tiles, AI-log path unusable or clipped | `settings-*`, `admin-users` (phone) | settings pages | Deferred: [#456](https://github.com/s3ntin3l8/sanctuary/issues/456) |
| 13 | P3 | Phone: Home KPI grid is 3+2, labels touch the tile edge, "Needs you" rows truncate to a few characters | `home.phone.*` | `HomePage.tsx` | Deferred: [#457](https://github.com/s3ntin3l8/sanctuary/issues/457) |
| 14 | P3 | No `prefers-reduced-motion` handling (pulse, spin, `passage-focus`) | Code read (no occurrence in `frontend/src`) | `index.css`, `Shell.tsx`, `DocumentReview.tsx` | Deferred: [#458](https://github.com/s3ntin3l8/sanctuary/issues/458) |

---

## Guardrails

- **Token contrast test** (`frontend/src/styles/contrast.test.ts`, runs in `make frontend-test`): every text token must reach 4.5:1 on all six surfaces, and `on-accent`/`on-danger`/`on-warning` on their fills, in both themes. Checked that it fails on the old `--warning` value.
- **axe in the capture config** (`"axe": true` in `docs/audit/capture.json`): a re-audit reports the same rules; the before/after of this audit is `color-contrast` 83 → 0 nodes, `link-in-text-block` 4 → 0.
- Not yet automated, tracked in [#459](https://github.com/s3ntin3l8/sanctuary/issues/459): an axe smoke test in CI over rendered pages, and an axe pass for the auth screens.

---

## Evidence

Regenerate (from the repo root; the capture tool is `ai-agent-skills` at `8a2b73c`):

```bash
npm --prefix frontend ci && npx --prefix frontend playwright install chromium   # once
node <ai-agent-skills>/plugins/audit/skills/audit/scripts/capture.mjs 2026-10-frontend-ui-ux
node <ai-agent-skills>/plugins/audit/skills/audit/scripts/capture.mjs 2026-10-frontend-ui-ux --config docs/audit/capture.auth.json
```

The first command writes 112 screenshots and `axe.json` into `docs/audit/assets/2026-10-frontend-ui-ux/` (gitignored; `axe.json` is rewritten by every pass that has `axe: true`, so move it aside to keep one). Overflow was measured by loading each route at 390px in the demo and comparing `documentElement.scrollWidth` with the viewport and listing controls whose right edge is past it together with the overflow of their ancestors.

---

## Deferred backlog

| Finding | Severity | Issue |
|---|---|---|
| Rail badge counts missing from accessible names | P2 | [#451](https://github.com/s3ntin3l8/sanctuary/issues/451) |
| Nested interactive controls in triage rows / graph relay nodes | P2 | [#452](https://github.com/s3ntin3l8/sanctuary/issues/452) |
| Duplicate `<main>`; `/search` without h1 | P2 | [#453](https://github.com/s3ntin3l8/sanctuary/issues/453) |
| Phone: scrollers without a hint; slice views | P2 | [#454](https://github.com/s3ntin3l8/sanctuary/issues/454) |
| Phone: case page header and graph | P2 | [#455](https://github.com/s3ntin3l8/sanctuary/issues/455) |
| Phone: settings nav, admin form, data tiles | P2 | [#456](https://github.com/s3ntin3l8/sanctuary/issues/456) |
| Phone: Home KPI grid and "Needs you" rows | P3 | [#457](https://github.com/s3ntin3l8/sanctuary/issues/457) |
| `prefers-reduced-motion` | P3 | [#458](https://github.com/s3ntin3l8/sanctuary/issues/458) |
| axe smoke test in CI; axe for the auth pass | P3 | [#459](https://github.com/s3ntin3l8/sanctuary/issues/459) |
