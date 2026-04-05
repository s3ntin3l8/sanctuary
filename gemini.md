# GEMINI.md: The Sanctuary Core Logic & Standards

## 1. Project Identity & Persona
* **Name:** The Sanctuary (Lead Counsel Edition).
* **Persona:** A high-precision legal-tech engineer focusing on information density, "Quiet Sanctuary" aesthetics, and logical document provenance.
* **Tone:** Professional, reliable, and minimalist.

## 2. Technical Stack (Non-Negotiable)
* **Backend:** Python 3.12+ / FastAPI.
* **Frontend:** HTMX (for all server communication) + Alpine.js (for local UI state like Focus Mode).
* **Styling:** Tailwind CSS v4 (Strictly following Stitch-provided design tokens).
* **Database:** SQLite with `sqlite-vec` extension for semantic search across 800+ PDFs.
* **AI Engine:** Local Ollama instance running **Qwen 3.5 9B** for summaries and extraction.
* **Ingestion:** **Docling** for PDF-to-Markdown conversion and layout analysis.

## 3. UI/UX Architecture
* **Layout:** Three-Pane Split View (18% Sidebar / 47% Stream / 35% Context).
* **States:** 1. `DEFAULT`: Full three-pane visibility.
    2. `FOCUS`: Sidebar collapsed to icons via Alpine.js `isFocusMode` toggle.
    3. `STREAM_ONLY`: Hides the Right Pane for deep reading of the chronology.
* **The Vertical Identity Header:** * Must remain `sticky top-0`.
    * Hierarchy: Case Title (XL Bold) -> Court ID (Mono) -> Internal ID (Mono).
    * Status Badge (Teal) anchored to the far right.

## 4. Document Logic: The "Russian Doll" Protocol
* **Nesting:** All documents must check for a `parent_id`. 
* **Indentation:** Child documents are indented `24px` with a 1px vertical 'L-connector' line.
* **Originator Stripes:** Every card must have a `border-l-4` color code:
    * `#0369A1` (Blue) = Court / Gavel Icon.
    * `#B91C1C` (Red) = Opposing Counsel / Warning Icon.
    * `#047857` (Green) = Your Lawyer / Shield Icon.
* **Provenance Rule:** Every card footer must state: *"Via: Email from [Sender] on [Date]"*.

## 5. Data Integrity & Formatting Rules
* **The H&M Rule:** **CRITICAL.** Every instance of the entity "H&M" (retail/clothing/expenses) must be rendered in **ALL CAPS**. This applies to database entries, AI summaries, and UI labels.
* **Management Summary:** Every document in the Right Pane must have a 3-bullet "Management Summary" (Legal Significance, Action/Deadline, Financial Impact).
* **Triage Logic:** Any document without a `case_id` or `parent_id` defaults to the **Triage Inbox** at the top of the stream.

## 6. Navigation & Routing Structure
* `/dashboard`: Global cross-case overview.
* `/triage`: Processing center for unlinked files.
* `/chronology`: The main "Russian Doll" feed.
* `/files`: Traditional directory navigation.
* `/costs`: Specialized view for invoices and **H&M** expense tracking.
* `/contacts`: Directory of all legal "Initial Senders."

## 7. Agent Workflow (Non-Negotiable)

For every task, follow this sequence strictly:

1. **Check `agent_task.md`** — read current state, identify target items
2. **Plan** — write implementation plan to `.opencode/plans/`
3. **Implement** — execute per plan
4. **Verify** — check each implementation item individually (syntax, imports, behavior)
5. **Final verification** — cross-file consistency, route uniqueness, integration checks
6. **Update `agent_task.md`** — mark items FIXED, update "What Has Been Built" and "Key Files"
7. **Commit** — descriptive commit message, one commit per logical package

## 7. Agent Workflow (Non-Negotiable)

For every task, follow this sequence strictly:

1. **Check `agent_task.md`** — read current state, identify target items
2. **Plan** — write implementation plan to `.opencode/plans/`
3. **Implement** — execute per plan
4. **Verify** — check each implementation item individually (syntax, imports, behavior)
5. **Final verification** — cross-file consistency, route uniqueness, integration checks
6. **Update `agent_task.md`** — mark items FIXED, update "What Has Been Built" and "Key Files"
7. **Commit** — descriptive commit message, one commit per logical package