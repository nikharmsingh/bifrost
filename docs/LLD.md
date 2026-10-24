# Low Level Design (LLD)

## 1. uv Workspace Mechanics

### How workspaces work

`uv` workspace mode is declared in the root `pyproject.toml`:

```toml
[tool.uv.workspace]
members = [
    "packages/*",   # shared libraries
    "mcp-servers/*",
    "apis/*",
    "agents/*",
    "apps/*",       # deployable apps
]
```

`uv` scans each glob, finds every `pyproject.toml` inside, and treats them as workspace members. All members share **one virtual environment** and **one lockfile** (`uv.lock`) at the root.

### Two types of workspace members

```
packages/core/   → package = true  (default)   → built as a wheel, importable
apps/hello-api/  → package = false             → deps installed, not built as wheel
```

**Libraries** (`packages/*`) are real Python packages. They are built into wheels and installed into the shared venv so other packages can `import` them.

**Apps** (`apps/*`, `apis/*`, `agents/*`) are runnable services. They consume libraries but are not imported by anyone. Marking them `package = false` tells uv: "install this member's dependencies but don't build a wheel for it."

```toml
# apps/hello-api/pyproject.toml
[tool.uv]
package = false   ← skip wheel build, just install deps
```

### Dependency resolution

```
uv lock              → resolves all packages across the entire workspace
                       writes a single uv.lock

uv sync              → installs everything from uv.lock into .venv/

uv run --package X   → runs a command using package X's entry point
```

Cross-package references use workspace sources:

```toml
# apis/gateway/pyproject.toml
dependencies = ["core", "llm"]

[tool.uv.sources]
core = { workspace = true }   ← resolves to packages/core/ locally
llm  = { workspace = true }   ← resolves to packages/llm/ locally
```

In production, these would be published to PyPI and referenced by version. In the workspace, `uv` links them directly.

---

## 2. Package Structure

### packages/core

Shared foundation used by all apps.

```
packages/core/
├── pyproject.toml
└── src/
    └── core/
        ├── __init__.py
        ├── config.py      ← Pydantic Settings (reads from .env)
        ├── logging.py     ← structured logger factory
        └── types.py       ← shared Pydantic models / type aliases
```

```python
# Usage in any app
from core.config import Settings
from core.logging import get_logger
from core.types import MessageRole
```

Hatchling auto-discovers the `core` package because `src/core/` matches the project name.

### packages/llm

Anthropic SDK wrapper with streaming helpers.

```
packages/llm/
├── pyproject.toml
└── src/
    └── llm/
        ├── __init__.py
        ├── client.py      ← creates Anthropic client from Settings
        ├── streaming.py   ← async generator over streamed tokens
        └── prompts/
            └── __init__.py
```

### packages/tools

Reusable tools passed to LangGraph agents.

```
packages/tools/
├── pyproject.toml
└── src/
    └── tools/
        ├── __init__.py
        ├── web_search.py  ← search tool
        ├── file_ops.py    ← read/write files tool
        └── memory.py      ← in-memory key-value store tool
```

---

## 3. App Structure

### apps/hello-api and apps/notes-api (Vercel)

```
apps/hello-api/
├── api/
│   └── index.py        ← FastAPI app (Vercel entry point)
└── pyproject.toml      ← package = false, deps: fastapi, uvicorn
```

Vercel's `@vercel/python` builder looks for the file at the path declared in `vercel.json` and wraps it as a serverless function. The FastAPI `app` object is the ASGI handler.

**Route layout rule for Vercel path routing:**

Because all requests arrive with the full path (e.g. `/notes/docs`), every FastAPI route and the `docs_url` / `openapi_url` must include the path prefix:

```python
# apps/notes-api/api/index.py
app = FastAPI(
    docs_url="/notes/docs",
    openapi_url="/notes/openapi.json",   # ← must start with /notes
)

@app.get("/notes")           # ← full path, not just "/"
@app.get("/notes/{id}")
```

Without this, Vercel's catch-all route intercepts the bare `/openapi.json` request and forwards it to the wrong app.

---

## 4. Vercel Deployment Design

### vercel.json (root)

```json
{
  "builds": [
    { "src": "apps/hello-api/api/index.py", "use": "@vercel/python" },
    { "src": "apps/notes-api/api/index.py", "use": "@vercel/python" }
  ],
  "routes": [
    { "src": "/notes(.*)", "dest": "apps/notes-api/api/index.py" },
    { "src": "/(.*)",      "dest": "apps/hello-api/api/index.py" }
  ]
}
```

`builds` — tells Vercel which Python files become serverless functions.
`routes` — evaluated top-to-bottom; first match wins. Specific prefixes go before the catch-all.

### .vercelignore

```
pyproject.toml
```

Excludes the root `pyproject.toml` from Vercel's build environment. Without this, Vercel detects the uv workspace and runs `uv sync` over the entire monorepo (installing Anthropic SDK, LangGraph, etc.). This is wasteful for a simple FastAPI function and caused a broken `dist-info` error from a transitive dependency (`annotated-doc 0.0.4`).

With it excluded, Vercel falls back to `pip install -r requirements.txt`, which installs only `fastapi` and `uvicorn`.

### requirements.txt (root)

```
fastapi>=0.115
uvicorn[standard]>=0.29
```

Used **only** by Vercel. Local development uses `uv sync` from `pyproject.toml`.

### Install flow comparison

```
Local dev:                          Vercel build:
  uv sync                             pip install -r requirements.txt
    └─ reads uv.lock                    └─ reads requirements.txt (root)
    └─ installs all workspace deps      └─ installs fastapi + uvicorn only
    └─ links packages/* as editable     └─ no workspace awareness
```

---

## 5. Adding a New App

### Step 1 — Create the app

```bash
mkdir -p apps/my-app/api
```

### Step 2 — pyproject.toml

```toml
[project]
name = "my-app"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.29"]

[tool.uv]
package = false
```

### Step 3 — FastAPI entry point

```python
# apps/my-app/api/index.py
from fastapi import FastAPI

app = FastAPI(
    docs_url="/my-app/docs",
    openapi_url="/my-app/openapi.json",
)


@app.get("/my-app")
async def root():
    return {"app": "my-app"}
```

### Step 4 — Wire into vercel.json

```json
{
  "builds": [
    { "src": "apps/hello-api/api/index.py",  "use": "@vercel/python" },
    { "src": "apps/notes-api/api/index.py",  "use": "@vercel/python" },
    { "src": "apps/my-app/api/index.py",     "use": "@vercel/python" }
  ],
  "routes": [
    { "src": "/notes(.*)",   "dest": "apps/notes-api/api/index.py" },
    { "src": "/my-app(.*)",  "dest": "apps/my-app/api/index.py" },
    { "src": "/(.*)",        "dest": "apps/hello-api/api/index.py" }
  ]
}
```

### Step 5 — Push

```bash
git add apps/my-app/ vercel.json
git commit -m "feat: add my-app"
git push
```

Vercel detects the push, rebuilds, and the new app is live at `/my-app`.

---

## 6. Inter-package Dependency Graph

```
packages/core  ◀─────────────────────────┐
     ▲                                    │
     │                                    │
packages/llm   ◀──────────────┐          │
     ▲                         │          │
     │                         │          │
packages/tools ◀──┐            │          │
                  │            │          │
            agents/            │          │
            ├── research-agent─┘          │
            └── coding-agent──────────────┘
                                          │
            apis/                         │
            ├── gateway───────────────────┘
            └── webhooks──────────────────┘

            apps/  (no shared package deps — standalone for Vercel)
            ├── hello-api
            └── notes-api
```

`apps/` are intentionally kept dependency-free from `packages/` so they can be deployed to Vercel without the full workspace. If an app needs shared code, extract it into a standalone pip-installable package and add it to `requirements.txt`.
