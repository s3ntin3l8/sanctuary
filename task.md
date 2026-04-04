# Task List - Sanctuary Legal Counsel

## Features Still to be Implemented

### 1. AI-Powered Management Summaries (High Priority)
Connect to local Ollama for AI summaries with 3-bullet insights:
- **Legal Significance:** What does this document legally mean for our position?
- **Required Action:** What do we need to do next and by when?
- **Financial Impact:** Are there cost implications (filing fees, discovery costs, settlement exposure)?

**Logic:** After Docling converts PDF to Markdown, send content to Ollama for analysis. Store structured JSON in document record. Display as collapsible AI summary in document views.

**Implementation:**
- Add `ai_summary` (Text), `ai_summary_created_at` (DateTime) to Document model
- Add `app/services/ai_summary.py` with Ollama integration
- Update ingestion pipeline to call AI summary after metadata extraction
- Add AI summary endpoint for standalone AI generation

### 2. Semantic Search with SQLite-Vec (High Priority)
Enable natural language search across document content using vector embeddings.

**Logic:** Store document embeddings in SQLite `sqlite-vec` extension. When user searches, embed query and perform nearest-neighbor similarity search. Returns ranked list of relevant documents.

**Implementation:**
- Add `vec` column type in Document table via migration
- Generate embeddings from document content using Ollama (e.g., `nomic-embed-text`)
- Create `/search?q=...` endpoint with similarity scoring
- Display results with highlights

### 3. PDF Preview in Contextual Workspace (Medium-High Priority)
Right-pane PDF viewer to read source documents while viewing extracted metadata/summaries in left pane.

**Logic:** Embed PDF viewer in iframe or use PDF.js. When document opened, show PDF in right column with scroll-syncing to highlighted sections in markdown view.

**Implementation:**
- Add PDF viewer component to document detail page
- Add PDF endpoint `/api/documents/{id}/pdf`
- Consider PDF text layer overlay for keyword highlighting

### 4. Global Entity Pivot (Medium Priority)
Cross-document aggregation view showing all people, deadlines, and expenses mentioned across all cases.

**Logic:** Parse entities from document content (potentially with AI). Aggregate by type (person, deadline, expense) and show as pivot table: "All instances of [John Smith]" with context snippets.

**Implementation:**
- Entity extraction pipeline (regex + NLP)
- Entity index table in SQLite
- `/entities` endpoint with filtering by type
- Link entities to their source documents

### 5. Focus Mode (Toggleable Sidebar) (Low Priority)
Collapsible sidebar for navigation that maximizes document viewing area.

**Logic:** JavaScript toggle button hides sidebar class on document views. Remembers state in localStorage.

**Implementation:**
- Add collapsible sidebar to templates
- JavaScript toggle logic
- CSS media queries for mobile

---

## Technical Optimizations

### 6. Self-Host Frontend Assets
Bring the frontend in line with the privacy-first promise by removing CDN/runtime dependencies.

**Current:** `base.html` loads Google Fonts, Alpine.js, and HTMX from external CDNs, and `sidebar.html` references a remote avatar image.
**Future:** Move required assets into `static/` and serve everything locally.

### 7. Add Real Database Migrations
Introduce Alembic before the schema grows further.

**Current:** Schema setup relies on `Base.metadata.create_all(...)` at startup.
**Future:** Add migration support for upcoming changes like `ai_summary`, vector columns, and entity tables.

### 8. Reduce Template-Driven DB Queries
Cut unnecessary session creation during page render.

**Current:** `sidebar_counts()` is called from Jinja and opens its own DB session; helper lookups such as case title/court ID also open extra sessions.
**Future:** Compute shared chrome/page context once in the route handler and pass it into templates.

### 9. Make Dashboard Data-Driven
Replace placeholder metrics and cards with live case/document data.

**Current:** Dashboard UI is visually built but mostly static.
**Future:** Populate metrics, action items, recent files, and hearings from the database.

### 10. Harden Ingestion Error Handling
Make upload/parse failures visible and recoverable.

**Current:** Docling conversion runs inline during upload with minimal resilience.
**Future:** Add try/except handling, failure states, fallback parsing, and better user feedback for corrupted or unsupported files.

### 11. Centralize Business-Rule Normalization
Apply core formatting/business rules consistently across the app.

**Current:** The H&M capitalization rule is enforced on `Expense`, but not yet centrally across ingestion, AI summaries, and all UI surfaces.
**Future:** Add a shared normalization utility and use it throughout ingestion, summarization, and rendering.

---

## To Be Implemented Later

### 12. Enhanced Metadata Extraction (Heuristic → Hybrid)
Improve regex-based extraction with machine learning or LLM-based entity extraction.

**Current:** Keyword matching and regex patterns (brittle, limited accuracy).
**Future:** Use AI to classify originator, extract dates, identify sender, auto-tag case relevance.

### 13. Automated Deadline Extraction
Extract court deadlines from document content and create calendar events.

**Current:** Manual entry only.
**Future:** AI identifies "shall respond within 14 days" and auto-creates deadline with due date calculation.

### 14. Document Comparison & Version History
Side-by-side comparison of document versions, highlight changes.

**Current:** No versioning (documents immutable once ingested).
**Future:** Track document updates, show diff view, maintain revision history.

### 15. Advanced Filtering & Saved Searches
Filter timeline/cases by multiple criteria, save frequent search queries.

**Current:** Basic filtering by status.
**Future:** Multi-criteria filtering (date range, originator, file type, review status), saved search queries.

### 16. API Documentation & Public Endpoints
OpenAPI/Swagger docs at `/docs` for external integrations.

**Current:** FastAPI has `/docs` but no public API documentation.
**Future:** Document all endpoints, add API keys for external clients, webhooks for ingest events.

### 17. Performance Optimizations
Pagination for timeline and case streams (800+ docs would be slow without it).

**Current:** No pagination, all results loaded at once.
**Future:** Server-side pagination, lazy loading, infinite scroll, database query optimization.

### 18. Test Suite
Comprehensive pytest coverage for heuristics, DB operations, API endpoints.

**Current:** No tests present.
**Future:** Unit tests for metadata extraction, integration tests for ingestion flow, E2E tests for UI.

### 19. Error Handling & Resilience
Graceful degradation when Docling fails to parse, retry mechanisms.

**Current:** No error handling around Docling.
**Future:** Try/except blocks, fallback parsers (e.g., text-only for corrupted PDFs), user notifications.

### 20. Database Connection Pooling
Centralize DB connection management for better performance under load.

**Current:** `get_db()` dependency on each endpoint (FastAPI style).
**Future:** Connection pool configuration, timeout handling, connection health checks.

### 21. Vertical Identity Header
Styling issue: Document header not properly aligned with originator color stripe.

**Current:** Visual inconsistency in document cards.
**Future:** CSS fixes to align header, ensure color stripe full-height, consistent typography.

---

## Notes

- **Privacy-Centric:** All AI features (Ollama) run locally, no external data transfer
- **Performance First:** SQLite-based for speed; consider PostgreSQL only if dataset exceeds 100K docs
- **Iterative:** Start with AI summaries and semantic search for maximum impact
