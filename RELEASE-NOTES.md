# Release Notes

All notable changes to Bifrost are documented here.

---

## [v0.11.0] — 2026-10-25

### Added
- **Saved history in Code Reviewer and Resume Analyzer.** Every successful review and analysis is saved to a shared Neon `ai_runs` table. A **Your history** panel lists each one with its score and date, reopens it (Code Reviewer also restores the code and language), and deletes it. The API is `GET <app>/history`, `GET <app>/history/{id}` and `DELETE <app>/history/{id}`.
- **Share links.** **Share link** creates an unguessable public, read-only URL (`<app>?shared=…`) and copies it, and **Stop sharing** revokes it. Resume Analyzer links show the job link and the analysis only, never the resume or its file name. The API is `POST`/`DELETE <app>/history/{id}/share` and the public `GET <app>/shared/{share_id}`.
- **Usage in Settings.** A new panel shows your last 30 days: requests, tokens, requests today, a per-day chart and a per-app table (`GET /auth/usage`).
- **Markdown notes.** The Notes editor has a live preview with **Write / Split / Preview** views (remembered per browser), sanitized with DOMPurify. marked and DOMPurify are loaded from cdnjs at pinned versions with SRI hashes.
- **Tags and pinning in Notes.** Tags are added comma-separated and appear as chips, with a tag filter row, and search matches `#tag`. Pinned notes appear first. The API adds `tags` and `pinned` to notes, plus `PATCH /notes/{id}` for partial updates. Pinning doesn't change a note's "updated" time.
- **Keyboard shortcuts in Notes.** `N` new note, `/` search, `J`/`K` next and previous, `E` edit, `P` pin, `V` change view, `⌘S` or `⌘Enter` save, `Esc` leave the field. Press `?` to see the list.

### Changed
- Deleting a run from history erases its code, inputs and result but keeps its token counts, so usage stays accurate.
- Code Reviewer and Resume Analyzer responses include a `run_id`. Model output is validated against the response schema before it's saved or returned.
- Code Reviewer and Resume Analyzer create the shared tables on startup, so history works on a fresh database.

### Upgrade notes
- No new settings are needed. On their first start, the apps create the `ai_runs` table and add the `tags` and `pinned` columns to `notes`. Existing notes keep their content, with no tags and unpinned.
- If an open Notes page looks unchanged after deploying, hard-refresh it (⌘⇧R).

---

## [v0.10.0] — 2026-10-25

### Added
- **Uptime history on the dashboard.** Each app card shows 24 h and 7 d uptime, the average latency of successful checks, and a 24-hour bar strip: green when every check passed, amber for partial failures, red when all failed, grey with no data. A new **Uptime (24h)** tile shows the overall figure.
- **`POST /status/check`** (`Authorization: Bearer $STATUS_CHECK_SECRET`) records every routed app's `/health` result in a Neon `health_checks` table, using the same discovery as the dashboard. Rows older than 8 days are pruned.
- **`GET /status`** returns, per app, 24 h and 7 d uptime, average latency, the last result and 24 hourly buckets. It's edge-cached for 60 s.
- **Status check workflow** (`.github/workflows/status-check.yml`) records a check every 10 minutes. It runs on GitHub Actions because Vercel's Hobby plan only runs crons once a day.

### Changed
- The dashboard's 🔒 tooltip now reads "Requires sign-in", replacing the outdated `X-API-Key` wording.

### Upgrade notes
- Set `STATUS_CHECK_SECRET` to the same value in Vercel and as a GitHub Actions secret.
- GitHub pauses scheduled workflows after 60 days without repo activity. Any push resumes it.

---

## [v0.9.0] — 2026-10-25

### ⚠️ Breaking
- **Code Reviewer and Resume Analyzer need sign-in and your own Groq key.** Each user saves a key once in `/settings`, and every request runs on that user's key, so the server's keys are never spent on visitors. Anonymous requests return `401`, and requests without a saved key return `428` pointing to Settings.
- **Resume Analyzer is Groq-only.** The Claude path, which used the server's Anthropic key, is removed.
- **Sign-in moved to `/auth/*` and covers the whole site.** The GitHub OAuth callback is now `https://<host>/auth/callback`. Existing Notes sessions ended, so users sign in once more.

### Added
- **Shared GitHub sign-in** (`packages/auth`, `bifrost_auth`). One `bifrost_session` cookie (`Path=/`) works across every app, and the post-login `?next=` redirect is restricted to paths on this site. Any app can add sign-in with `Depends(auth.current_user)`.
- **Bring-your-own keys.** Keys are validated with Groq before saving, then stored **AES-GCM-encrypted** under `KEY_ENCRYPTION_SECRET`, with the user ID bound into the encryption. Only the last 4 characters are ever shown again. The API is `GET /auth/keys` and `PUT`/`DELETE /auth/keys/{provider}`.
- **Settings page** at `/settings` for your account and Groq key. The dashboard's new account chip has **Sign in**, **Settings** and **Sign out**.
- **Access banners** in Code Reviewer and Resume Analyzer that explain "sign in" or "add your key" and disable the action until both are in place.

### Changed
- **Faster pages.** UI pages are served from Vercel's regional edge cache for 5 minutes (stale-while-revalidate 1 day), and `/registry` for 30 seconds. Browsers always revalidate, and personal endpoints are never cached. In testing from India, cached page loads dropped from about 330 ms (spiking to about 1 s on cold starts) to about 110 ms.
- **Notes loads your profile and notes in parallel.**
- **Repo cleanup.** Removed the unused `packages/tools`, the `apis/webhooks` and `mcp-servers/knowledge-base` stubs, `scripts/seed_db.py`, `apps/hello-api/vercel.json`, the three deploy workflows that didn't deploy anything, and the stale `docs/`. The README is rewritten to match the repo, with verified commands. The agent graph check moved into pytest. The repo went from 83 to 64 tracked files.
- **Smarter requirements guard.** It follows workspace packages, so a dependency of `bifrost_auth` that's missing from an app's `requirements.txt` fails CI.
- **Smaller Vercel bundle:** `anthropic` removed, `cryptography` added.

### Upgrade notes
- Set `KEY_ENCRYPTION_SECRET` (32 random bytes, urlsafe base64) and **never change it**, or saved keys become unreadable.
- Point the GitHub OAuth app's callback at `/auth/callback`.
- `GROQ_API_KEY` and `ANTHROPIC_API_KEY` are no longer read by any deployed app and can be removed.

---

## [v0.8.0] — 2026-10-25

### ⚠️ Breaking
- **Notes API is now private and per-user.** Every endpoint requires GitHub sign-in, and each user sees only their own notes; another user's note returns `404`. The public `GET /notes` listing and the shared `X-API-Key` (`NOTES_API_KEY`) are removed. Notes is browser-only: there is no token or programmatic access.

### Added
- **Live dashboard** at `/`. It lists every deployed app with live health, latency, version and endpoints (🔒 marks endpoints that need sign-in), plus links to each app's UI and API docs, the latest release and CI status. Apps are discovered from `vercel.json` and each app's `openapi.json`, so a new app appears automatically. The raw data is at `/registry`.
- **Code Reviewer UI** at `/code-review`. Paste code, pick a language, and get a score, a recommendation, severity-sorted issues with fixes, and strengths. Includes a sample snippet and a ⌘/Ctrl+Enter shortcut.
- **Notes UI** at `/notes/ui`: a searchable note list and editor with GitHub sign-in, unsaved-change guards and ⌘/Ctrl+S.
- **GitHub sign-in for Notes** (OAuth). The session is a signed, httpOnly, `Secure`, `SameSite=Lax` cookie, and a signed `state` cookie protects the login flow against CSRF. Notes created before accounts existed are assigned to `NOTES_OWNER_GITHUB_LOGIN` on their first sign-in.
- **`/notes/health`**, which checks the database.
- **Requirements guard test.** CI fails if a Vercel app imports a package that's missing from the `requirements.txt` Vercel installs for it.
- **Neon preview cleanup workflow.** Closing a PR deletes its `preview/<branch>` Neon database branch, so previews no longer hit Neon's branch cap.

### Fixed
- **Code Reviewer returned 500 on every review.** Groq shut down `llama-3.3-70b-versatile` on 2026-08-16. Code Reviewer and Resume Analyzer's Groq fallback now default to `openai/gpt-oss-120b`, which can be overridden with `GROQ_MODEL`. Groq API errors now return `502` with a message.
- **Landing page outage after the dashboard deploy.** hello-api installs its dependencies from `apps/hello-api/requirements.txt`, which was missing `httpx`. The new requirements guard test now covers this.

### Changed
- App descriptions were added to Notes, Code Reviewer and Resume Analyzer, and they show on the dashboard.
- `.gitignore` now covers the per-developer Neon agent tooling: `.mcp.json`, `.claude/skills/` and `skills-lock.json`.

### Upgrade notes
- Set `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `SESSION_SECRET` and `NOTES_OWNER_GITHUB_LOGIN` for Notes sign-in. The OAuth app's callback URL must be `https://<host>/notes/auth/callback`. `NOTES_API_KEY` is no longer used and can be removed.
- For the Neon cleanup workflow, set the repo variable `NEON_PROJECT_ID` and the secret `NEON_API_KEY`.

---

## [v0.7.0] — 2026-10-24

### Security
- **Resume Analyzer XSS** — analysis results are now rendered with `textContent` instead of `innerHTML`. Model output is derived from untrusted job pages, so a crafted posting could previously inject script into the UI.
- **Resume Analyzer SSRF guard** — job posting URLs must be `http(s)` and resolve only to public IP addresses. Redirects are followed manually (max 5) and every hop is re-checked, so internal/metadata addresses can't be reached via a redirect.
- **Gateway auth & model allowlist** — `/chat/stream` requires an `X-API-Key` header when `GATEWAY_API_KEY` is set. Callers may only use `DEFAULT_MODEL` or models listed in `ALLOWED_MODELS`; prompt and system-prompt lengths are capped.
- **Notes API write protection** — create/update/delete require an `X-API-Key` header when `NOTES_API_KEY` is set. Reads stay public.
- **Code Runner MCP** — documented that it is not a sandbox, capped the timeout at 30s, and handled timeouts instead of crashing the tool.

### Fixed
- **Notes API 500 errors** — SQLAlchemy 2.1 made psycopg 3 the default Postgres driver, so `/notes` failed with `No module named 'psycopg'`. Switched from `psycopg2-binary` to `psycopg[binary]`.
- **Event loop blocking** — synchronous Groq/Anthropic/SQLAlchemy calls no longer run inside `async def` handlers (Code Reviewer, Notes API, Resume Analyzer).
- **`ALLOWED_ORIGINS` parsing** — `core.Settings` now accepts comma-separated values (the same format the Vercel apps use); the previously documented plain-URL value failed to parse.

### Added
- **CI workflow** (`.github/workflows/ci.yml`) — runs `ruff check`, `ruff format --check`, and `pytest` on every pull request and push to `main`.
- **Test suite** (`tests/`) — 17 tests covering the SSRF guard, XSS fix, API-key checks, model allowlist, settings parsing, and the Postgres driver.
- **Configurable models** — `DEFAULT_MODEL` (gateway, agents, `llm` package) and `ANTHROPIC_MODEL` (Resume Analyzer). Default is unchanged.
- **`uv.lock` committed** — Dockerfiles use `uv sync --frozen`, which requires it.

### Changed
- `main` is now a protected branch: changes land via pull request with a passing `check` job.
- Applied `ruff format` repo-wide; `.env.example` documents `GROQ_API_KEY`, `DATABASE_URL`, and the new API-key/model settings.
- `.gitignore` now covers `.vercel/`, `.claude/settings.local.json`, and extra tooling caches.

### Upgrade notes
- Set `NOTES_API_KEY` (and `GATEWAY_API_KEY` if you deploy the gateway) — until set, those endpoints remain unauthenticated.

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
