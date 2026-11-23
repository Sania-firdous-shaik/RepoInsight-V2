# Build status

Updated: 2026-11-12

## Completed

- FastAPI backend with account authentication, private per-user repository access, PostgreSQL records, Qdrant vector storage, Tree-sitter parsing, CPU embeddings, and Groq generation.
- Safe public-repository indexing with file/secret filters, size and count limits, deterministic index generations, stage-then-switch activation, and cleanup of stale generations only after activation.
- Retrieval upgrade with complete paginated repository scans, repository and generation isolation, dense plus BM25-style lexical ranking, exact identifier/path/endpoint signals, reciprocal-rank fusion, duplicate and overlap suppression, per-file diversity, adaptive result limits, and bounded citation-preserving context.
- Source-grounded Q&A, hybrid semantic search, commit-bound file explanations with section-oriented evidence, architecture generation, and repository documentation generation.
- Unified Code Explorer for searchable file browsing, syntax-highlighted source, per-file explanations, and per-file summaries; old file explanation routes redirect while preserving the selected file.
- Dedicated AI Summarization page for repository, folder, file, and module scopes. Large-scope summaries combine bounded source-file notes into module and repository summaries and are cached per commit and index generation.
- Markdown rendering with GFM support and HTML sanitization; inline citations display file and line ranges and navigate to the selected source. Sidebar, breadcrumbs, overview links, and search actions use consolidated Code Explorer navigation.
- AI Code Review route and dashboard with source-only file selection, six review categories, selected-file reviews, controlled repository batches, severity/category/file filters, validated line evidence, source-derived snippets, suggested fixes, reviewed state, and commit-aware review history. Suspected credential values are redacted from AI context and review evidence.
- Repository review jobs use a configurable process-wide Groq request gate (default one in-flight request with pacing), bounded Retry-After/exponential backoff with jitter, sibling-file batching, duplicate/generated-file skips, and persistent per-batch progress. Rate-limit interruptions preserve deterministic and completed AI findings and can resume without repeating successful batches; the dashboard polls deterministic, AI, waiting, skipped, and rate-limited file counts.
- Responsive frontend routes for overview, AI code assistant, Code Explorer, AI Summarization, semantic code search, architecture explorer, documentation generator, repository management, and settings.
- README, environment template, initial PostgreSQL migration, and additive index-generation migration.
- Read-only Hastakala benchmark script comparing the prior dense/limited-lexical baseline and the new retrieval, with optional Groq generation/citation inspection.

## Validation performed

- `frontend/npm run build`: passed. Vite reports a large optional Mermaid ELK chunk (about 1.5 MB).
- `backend/.venv/bin/python -m compileall -q app tests`: passed.
- `backend/.venv/bin/python -m unittest discover -s tests -v`: 32 tests passed for parser/chunker behavior, retrieval ranking/isolation, pagination, diversity, context citation budgeting, summary helpers, review validation/redaction, Groq 429 retry/pacing, and resume behavior.
- `backend/.venv/bin/alembic heads`: one head, `0002_index_generation`.
- `backend/.venv/bin/alembic upgrade head --sql`: generated offline SQL; the upgrade adds a nullable `repositories.index_generation` column.
- FastAPI route import: succeeded; routes include repository search, file explanation, and AI review plus history.
- Browser verification: Code Explorer shows the file tree, source editor, and AI Insights vertically; selected Checkout.jsx loads 883 indexed lines, file summary generated, and a rendered citation navigated to Code Explorer at the cited line.
- PostgreSQL and Qdrant integration and the Hastakala benchmark were not run in the unit-test environment. Browser review verification created a review-history record in the already running local development database; repository source and Qdrant index were not modified.
- Browser verification: AI Code Review route, repository context, source-file selector, category selector, and review result layout loaded. A selected-file request entered the Groq-backed review loading state but did not complete during this check.

## To run locally

1. Configure `.env` from `.env.example` and set the required secrets and Groq settings.
2. Start services with `docker compose up -d`.
3. In `backend/`, install requirements, run `alembic upgrade head`, then start `uvicorn app.main:app --reload --port 8000`.
4. In `frontend/`, run `npm install && npm run dev` and open http://localhost:5173.
5. Existing repository records and Qdrant points are retained. Their active index pointer starts as `NULL` and the retrieval layer supports legacy untagged points. A later successful reindex stages and activates a generation.

## Known limitations

- Indexing still runs in an in-process FastAPI background task and is not durable across backend restarts.
- Repository review progress is persisted and resumable, but its worker is also in-process; a stopped worker is detected by a 10-minute stale heartbeat before the job can be resumed. The configurable request gate is per backend process.
- Public GitHub repositories only; private repository OAuth is not implemented.
- Incremental Git-diff indexing and a full dependency graph are not implemented.
- Production deployment still needs TLS, operational rate limiting, backups, and deployment-specific secret management.
