# ⚡ Bifrost

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white" alt="Python 3.12"/>
  <img src="https://img.shields.io/badge/package_manager-uv-violet?logo=astral&logoColor=white" alt="uv"/>
  <img src="https://img.shields.io/badge/deployed_on-Vercel-black?logo=vercel&logoColor=white" alt="Vercel"/>
  <img src="https://img.shields.io/github/v/release/nikharmsingh/bifrost?color=indigo&label=release" alt="Release"/>
  <img src="https://img.shields.io/github/actions/workflow/status/nikharmsingh/bifrost/ci.yml?branch=main&label=CI&logo=githubactions&logoColor=white" alt="CI"/>
</p>

<p align="center">
  A <code>uv</code>-powered Python monorepo of small AI apps on Vercel — plus a shared core, LangGraph agents and an MCP server.
</p>

---

## Live apps

**Dashboard: [bifrost-api.vercel.app](https://bifrost-api.vercel.app/)** — every app with live health, latency, endpoints and links. It's built from `vercel.json` and each app's `openapi.json`, so a newly routed app appears automatically ([raw data](https://bifrost-api.vercel.app/registry)).

| App | UI | What it does | Access |
|---|---|---|---|
| **Code Reviewer** | [`/code-review`](https://bifrost-api.vercel.app/code-review) | Paste code → score, severity-sorted issues with fixes, strengths | Sign-in + your Groq key |
| **Resume Analyzer** | [`/resume-analyzer`](https://bifrost-api.vercel.app/resume-analyzer) | PDF resume + job URL → match score, gaps, keywords | Sign-in + your Groq key |
| **Notes** | [`/notes/ui`](https://bifrost-api.vercel.app/notes/ui) | Private notes in Neon Postgres | GitHub sign-in |

**One sign-in for every app:** sign in once (dashboard or any app) via GitHub at `/auth/login`; the `bifrost_session` cookie covers the whole site.

**Bring your own key:** the AI apps run on each user's own Groq key, saved once in [`/settings`](https://bifrost-api.vercel.app/settings) — checked with Groq, then stored AES-GCM-encrypted and never shown again. The server's own LLM keys are never used for visitors.

Each app's API docs are at `<path>/docs`, e.g. [`/code-review/docs`](https://bifrost-api.vercel.app/code-review/docs).

## Structure

```
bifrost/
├── apps/                  # Deployed to Vercel — one FastAPI function each
│   ├── hello-api/         #   Dashboard (/), /registry and shared sign-in (/auth/*)
│   ├── code-reviewer/
│   ├── resume-analyzer/
│   └── notes-api/         #   Per-user notes
├── packages/              # Shared libraries (uv workspace members)
│   ├── auth/              #   Shared GitHub sign-in (bifrost_auth)
│   ├── core/              #   Settings, logging, types
│   └── llm/               #   Anthropic client + streaming
├── apis/gateway/          # Streaming chat API (local / Docker; not deployed)
├── agents/                # LangGraph examples (local)
│   ├── research-agent/    #   researcher → synthesizer
│   └── coding-agent/      #   coder → reviewer
├── mcp-servers/
│   └── code-runner/       # Runs Python snippets — NOT a sandbox, local stdio only
├── tests/                 # pytest suite run by CI
└── vercel.json            # Builds + routes for every app
```

## How deployment works

- **Vercel** builds every entry in [`vercel.json`](vercel.json) on each push; `main` deploys to production, PRs get previews.
- **Routes** map a path prefix to an app; the `/(.*)` catch-all (hello-api) must stay last.
- **Dependencies** are installed from the `requirements.txt` nearest each app: `apps/hello-api/requirements.txt` for hello-api, the root [`requirements.txt`](requirements.txt) for the rest — *not* from `pyproject.toml`. CI fails if an app imports something its file doesn't list.
- **Database:** the Vercel–Neon integration gives each preview its own `preview/<branch>` database branch; [a workflow](.github/workflows/neon-preview-cleanup.yml) deletes it when the PR closes, so previews never hit Neon's branch cap.
- **`main` is protected:** changes land via PR with a passing `check` job ([`ci.yml`](.github/workflows/ci.yml): ruff + pytest).

## Local development

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv
git clone https://github.com/nikharmsingh/bifrost && cd bifrost
cp .env.example .env                               # fill in what you need
uv sync --all-packages                             # one venv for the whole workspace

uv run pytest                                      # tests
uv run ruff check . && uv run ruff format .        # lint + format
```

Run a Vercel app locally (from its `api/` folder):

```bash
cd apps/code-reviewer/api && uv run uvicorn index:app --reload
```

Other components:

```bash
cd apis/gateway && uv run uvicorn src.main:app --reload        # gateway
cd agents/research-agent && uv run python -m src.main "What is LangGraph?"   # agent
```

The code-runner MCP server for Claude Code / Claude Desktop:

```json
{
  "mcpServers": {
    "code-runner": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/bifrost/mcp-servers/code-runner", "run", "python", "src/server.py"]
    }
  }
}
```

## Adding a Vercel app

1. Create `apps/<name>/api/index.py` with a FastAPI app whose `docs_url` / `openapi_url` include the prefix (e.g. `/my-app/docs`), plus a `GET /my-app/health`.
2. Add a `pyproject.toml` (copy one from `apps/`), and add any new dependencies to the root `requirements.txt`.
3. In `vercel.json`, add a `builds` entry and a route **above** the `/(.*)` catch-all.
4. Need users? Load `bifrost_auth` the way `apps/notes-api/api/index.py` does and use `Depends(auth.current_user)` — it reads the shared session.
5. Add tests under `tests/`, open a PR. Once merged, the dashboard lists the app automatically.

## Configuration

All settings are environment variables — see [`.env.example`](.env.example) for the full list with comments. Production values live in Vercel (`vercel env ls`).

| App | Needs |
|---|---|
| Code Reviewer, Resume Analyzer | Sign-in settings + `KEY_ENCRYPTION_SECRET` (users supply their own Groq key; optional `GROQ_MODEL`) |
| Sign-in (every app using it) | `DATABASE_URL`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `SESSION_SECRET`; GitHub OAuth callback `https://<host>/auth/callback` |
| Notes | the sign-in settings, plus `NOTES_OWNER_GITHUB_LOGIN` |

## Releases

Bump [`VERSION`](VERSION), add an entry to [`RELEASE-NOTES.md`](RELEASE-NOTES.md), merge, then push a `vX.Y.Z` tag — the release workflow creates the GitHub release. The dashboard shows the current version.
