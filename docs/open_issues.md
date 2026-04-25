## UX gaps

| # | Gap |
|---|---|
| A | Entities stage produces data but has zero UI surface — no HUD partial, not in the stepper. |
| B | Significance tier and document type are read-only in review mode (must be editable selects to act on AI mistakes). |
| C | Claims / grounds / entities / action items commit silently — no per-item confirm/reject affordance. |
| D | Triage card delete uses `window.location.reload()` instead of an OOB swap. |
| E | No destination feedback after bundle confirm (no toast, no case link). |
| F | No bulk operations in the triage queue. |
| G | No "failed" count in the triage status bar. |
| H | No model-switch shortcut from the AI-failure banner. |
| I | Upload feedback: ~400ms blind redirect, no per-file progress. |
| J | No first-run / empty-state guidance on `/triage`. |

- UI feature to handle open document tasks within a case (not on new documents which is covered by triage but on existing documents)
- UI feature to edit metadata of existing documents
- Multi-target relationship UX (one source doc with several REPLIES_TO edges to different parents) — the helper handles each target independently, no special case.
- UI elements to edit relationships / passages / claims
