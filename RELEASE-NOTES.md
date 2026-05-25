# Release Notes

All notable changes to Bifrost are documented here.

---

## [v0.6.1] — 2026-05-25

### Fixed
- **Vercel build stability** — reverted experimental per-app `requirements.txt` split and builder-version pinning that caused cascading `ENOENT` build failures across all functions. Restored the single root `requirements.txt` with the original working package set.

---

## [v0.6.0] — 2026-05-25

### Added
- **Resume Analyzer** (`apps/resume-analyzer/`) — end-to-end app deployed at `/resume-analyzer`.
  - `GET /resume-analyzer` — interactive dark-mode HTML UI with drag-and-drop PDF upload and job posting URL input.
  - `POST /resume-analyzer/analyze` — accepts a PDF resume + job posting URL; extracts PDF text with `pypdf`, scrapes the job page with `httpx` + `BeautifulSoup`, and returns a structured analysis: match score (0–100), verdict, strengths, gaps, actionable suggestions, and matched/missing keywords.
  - **Dual-provider LLM support** — uses Anthropic Claude Sonnet (`tool_use` with forced tool call for guaranteed structured output) when `ANTHROPIC_API_KEY` is set; falls back to Groq `llama-3.3-70b-versatile` (JSON mode) when only `GROQ_API_KEY` is set. Anthropic takes precedence when both keys are present.
- **Resume Analyzer** added to the hello-api service registry on the Bifrost landing page.

### Changed
- `requirements.txt` — added `anthropic`, `httpx`, `pypdf`, `beautifulsoup4`, `python-multipart` for the new app.
- `vercel.json` — added build entry and `/resume-analyzer(.*)` route for the new app.

---

## [v0.5.0] — 2026-05-24

### Added
- **Async streaming** — gateway `/chat/stream` now uses a true async generator backed by `AsyncAnthropic`, so it no longer blocks the event loop.
- **Prompt caching** — system prompts in both `stream_text` and `stream_text_async` are sent with `cache_control: ephemeral`, enabling Anthropic prompt cache hits and reducing latency + cost on repeated calls.
- **`stream_text_async`** — new async streaming helper in `packages/llm` alongside the existing sync version.
- **`CodingState` TypedDict** — extracted to `agents/coding-agent/src/state.py`; node functions are now properly typed (was plain `dict`).

### Changed
- **Lazy model init in agents** — `_model` module-level instantiation replaced with `@lru_cache _get_model()` in both research-agent and coding-agent; API key is now read on first call, not at import time.
- **Configurable CORS origins** — gateway reads `allowed_origins` from `Settings`; notes-api reads `ALLOWED_ORIGINS` env var. Both default to `*` in development.
- **Notes-api input validation** — `NoteIn.title` capped at 200 chars (min 1); `NoteIn.content` capped at 50,000 chars.
- **Landing page version** — `⚡ Bifrost API` header now reads version from `app.version` instead of a hardcoded string; bumped to `0.5.0`.

---

## [v0.4.0] — 2026-05-24

### Added
- **Neon PostgreSQL** — notes-api now persists data in a real database instead of in-memory storage. Uses SQLAlchemy 2.0 + psycopg2-binary with `NullPool` (serverless-safe). Table auto-created on cold start.
- **Vercel Analytics** — injected `/_vercel/insights/script.js` into the HTML landing page to track page views and visitors.
- **HTML Landing Page** — root `/` now serves a styled dark-mode HTML page listing all live services with links to their Swagger docs, replacing the plain JSON registry.
- **Vercel Speed Insights** — injected `/_vercel/speed-insights/script.js` to track Core Web Vitals (LCP, FID, CLS).

### Changed
- **Project renamed** from `python-turborepo` to **Bifrost** across GitHub repo, pyproject.toml, README, docs, config, and API registry.
- **Vercel URL** updated to `bifrost-api.vercel.app`.

---

## [v0.3.0] — 2026-05-24

### Added
- **API Registry** — `GET /` returns a live list of all deployed services with base paths, docs URLs, and available endpoints.

---

## [v0.2.0] — 2026-05-24

### Added
- **Code Reviewer** (`apps/code-reviewer/`) — `POST /code-review` accepts code + language and returns a structured JSON review with score (1–10), issues (type/severity/description/suggestion), strengths, and recommendation. Powered by Groq's free tier using `llama-3.3-70b-versatile`.
- **Automated release workflow** — pushing a `v*.*.*` tag now automatically creates a GitHub release with a changelog generated from commits since the previous tag.

### Changed
- Switched from Anthropic SDK to **Groq** (free tier) for the code reviewer to remove cost dependency.

---

## [v0.1.0] — 2026-05-24

### Added
- **Initial monorepo scaffold** using `uv` workspaces.
- **apps/hello-api** — quotes, UUID, and timestamp endpoints. Live at `/`.
- **apps/notes-api** — full CRUD notes API (in-memory at this point). Live at `/notes`.
- **apis/gateway** — streaming chat endpoint via Anthropic SDK.
- **apis/webhooks** — webhook event receiver.
- **agents/research-agent** — LangGraph pipeline: researcher → synthesizer.
- **agents/coding-agent** — LangGraph pipeline: coder → reviewer.
- **mcp-servers/knowledge-base** — search tool + status resource via FastMCP.
- **mcp-servers/code-runner** — executes Python snippets in a subprocess via FastMCP.
- **packages/core** — Pydantic settings, structured logging, shared types.
- **packages/llm** — Anthropic client + streaming helpers.
- **packages/tools** — web search, file ops, memory tools.
- **Vercel multi-app routing** via root `vercel.json`.
- **Dockerfiles** for all APIs, agents, and MCP servers.
- **GitHub Actions CI** for gateway, research-agent, coding-agent.
- **Docs** — HLD, LLD, and GUIDE covering architecture and developer workflows.
