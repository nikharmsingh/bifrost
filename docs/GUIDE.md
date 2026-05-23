# Developer Guide

Step-by-step instructions for every type of work in this monorepo.

---

## Table of Contents

1. [Setup](#1-setup)
2. [Apps — Vercel Serverless](#2-apps--vercel-serverless)
3. [Agents — LangGraph](#3-agents--langgraph)
4. [APIs — FastAPI + Docker](#4-apis--fastapi--docker)
5. [MCP Servers](#5-mcp-servers)
6. [Shared Packages](#6-shared-packages)

---

## 1. Setup

```bash
# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install all workspace deps
git clone https://github.com/nikharmsingh/python-turborepo
cd python-turborepo

# Copy env and fill in your Anthropic API key
cp .env.example .env

# Install everything (all packages, all apps)
uv sync
```

Your `.env` should look like:

```env
ANTHROPIC_API_KEY=sk-ant-...
LOG_LEVEL=INFO
ENV=development
```

---

## 2. Apps — Vercel Serverless

Apps live in `apps/`. They are stateless FastAPI services deployed as Vercel serverless functions.

**When to use:** public-facing APIs, lightweight endpoints, anything that doesn't need a database or long-running process.

### Create a new app

#### Step 1 — Scaffold

```bash
mkdir -p apps/my-app/api
```

#### Step 2 — pyproject.toml

```toml
# apps/my-app/pyproject.toml
[project]
name = "my-app"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.29",
]

[tool.uv]
package = false          # apps are not installable packages
```

> `package = false` is required for all apps. Without it, Vercel's build will fail trying to build a wheel.

#### Step 3 — FastAPI entry point

```python
# apps/my-app/api/index.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="My App",
    version="0.1.0",
    docs_url="/my-app/docs",           # must include the path prefix
    openapi_url="/my-app/openapi.json", # must include the path prefix
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/my-app")
async def root() -> dict:
    return {"app": "my-app", "status": "ok"}


@app.get("/my-app/health")
async def health() -> dict:
    return {"status": "ok"}
```

> **Path prefix rule:** because Vercel routes `/my-app(.*)` to this function, every route and both `docs_url`/`openapi_url` must start with `/my-app`. Without this, Swagger UI loads but shows the wrong API's schema.

#### Step 4 — Register in vercel.json

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

> Routes are evaluated top-to-bottom. Specific prefixes go above the catch-all `(.*)`.

#### Step 5 — Run locally

```bash
uv sync
uv run --package my-app uvicorn api.index:app --reload --app-dir apps/my-app
```

Visit `http://localhost:8000/my-app/docs`

#### Step 6 — Deploy

```bash
git add apps/my-app/ vercel.json
git commit -m "feat: add my-app"
git push
```

Vercel auto-redeploys on every push to `main`. Your app is live at:
`https://python-turborepo.vercel.app/my-app`

### Live apps

| Path | App | Docs |
|---|---|---|
| `/` | hello-api | `/docs` |
| `/notes` | notes-api | `/notes/docs` |

---

## 3. Agents — LangGraph

Agents live in `agents/`. They are multi-step AI pipelines built with LangGraph. Each agent is a graph of nodes where each node is a function that reads from and writes to a shared state.

**When to use:** multi-step reasoning, research, code generation, any task that needs more than one LLM call in sequence.

### How the graph pattern works

```
State (TypedDict)
  ↓
Node A  →  Node B  →  Node C  →  END
  ↑              ↑
  reads/writes State
```

Each node is a plain Python function:
```python
def my_node(state: MyState) -> MyState:
    # read from state
    result = llm.invoke(state["input"])
    # return updated state
    return {**state, "output": result.content}
```

### Run existing agents

```bash
# Research agent — researcher → synthesizer
uv run --package research-agent python -m src.main "What is LangGraph?"

# Coding agent — coder → reviewer
uv run --package coding-agent python -m src.main "Write a Python rate limiter"
```

### Create a new agent

#### Step 1 — Scaffold

```bash
mkdir -p agents/my-agent/src
touch agents/my-agent/src/__init__.py
```

#### Step 2 — pyproject.toml

```toml
# agents/my-agent/pyproject.toml
[project]
name = "my-agent"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "langgraph>=0.2",
    "langchain-anthropic>=0.1",
    "core",
]

[tool.uv.sources]
core = { workspace = true }

[tool.uv]
package = false
```

#### Step 3 — State

```python
# agents/my-agent/src/state.py
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


class MyState(TypedDict):
    messages: Annotated[list, add_messages]
    input: str
    draft: str
    output: str
```

#### Step 4 — Nodes

```python
# agents/my-agent/src/nodes.py
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from core.config import get_settings

_model = ChatAnthropic(model="claude-sonnet-4-6", api_key=get_settings().anthropic_api_key)


def drafter(state: dict) -> dict:
    response = _model.invoke([HumanMessage(content=f"Draft a response for: {state['input']}")])
    return {**state, "draft": response.content}


def refiner(state: dict) -> dict:
    response = _model.invoke([HumanMessage(content=f"Refine this draft:\n\n{state['draft']}")])
    return {**state, "output": response.content}
```

#### Step 5 — Graph

```python
# agents/my-agent/src/graph.py
from langgraph.graph import StateGraph, END
from .state import MyState
from .nodes import drafter, refiner

builder = StateGraph(MyState)
builder.add_node("drafter", drafter)
builder.add_node("refiner", refiner)

builder.set_entry_point("drafter")
builder.add_edge("drafter", "refiner")
builder.add_edge("refiner", END)

graph = builder.compile()
```

#### Step 6 — CLI entrypoint

```python
# agents/my-agent/src/main.py
import sys
from .graph import graph


def run(input_text: str) -> str:
    result = graph.invoke({"input": input_text, "messages": [], "draft": "", "output": ""})
    return result["output"]


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]) or "Hello, agent!"
    print(run(text))
```

#### Step 7 — Run it

```bash
uv sync
uv run --package my-agent python -m src.main "Your input here"
```

### Deploy agents

Agents are long-running processes — **not suitable for Vercel**. Deploy them as Docker containers.

#### Dockerfile

```dockerfile
# agents/my-agent/Dockerfile
FROM python:3.12-slim
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages/core  ./packages/core
COPY agents/my-agent ./agents/my-agent

RUN pip install uv && \
    uv sync --package my-agent --frozen --no-dev

CMD ["uv", "run", "--package", "my-agent", "python", "-m", "src.main"]
```

#### Build and run

```bash
docker build -f agents/my-agent/Dockerfile -t my-agent .
docker run --env-file .env my-agent "What is LangGraph?"
```

#### Expose as an HTTP service (optional)

Wrap the agent in a FastAPI endpoint so it can receive HTTP requests:

```python
# agents/my-agent/src/server.py
from fastapi import FastAPI
from pydantic import BaseModel
from .graph import graph

app = FastAPI()

class RunRequest(BaseModel):
    input: str

@app.post("/run")
async def run(req: RunRequest) -> dict:
    result = graph.invoke({"input": req.input, "messages": [], "draft": "", "output": ""})
    return {"output": result["output"]}
```

```dockerfile
CMD ["uv", "run", "--package", "my-agent", "uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 4. APIs — FastAPI + Docker

APIs live in `apis/`. Unlike `apps/`, they can use shared workspace packages (`core`, `llm`, `tools`) and are deployed as long-running Docker containers (not Vercel functions).

**When to use:** internal services, APIs that import shared packages, APIs that need persistent connections, websockets, or background tasks.

### Existing APIs

| Package | Path | Purpose |
|---|---|---|
| `gateway` | `apis/gateway/` | Main backend — chat streaming via `llm` package |
| `webhooks` | `apis/webhooks/` | Receives and routes webhook events |

### Create a new API

#### Step 1 — Scaffold

```bash
mkdir -p apis/my-api/src/routers
touch apis/my-api/src/__init__.py
touch apis/my-api/src/routers/__init__.py
```

#### Step 2 — pyproject.toml

```toml
# apis/my-api/pyproject.toml
[project]
name = "my-api"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "core",         # shared settings + logging
]

[tool.uv.sources]
core = { workspace = true }

[tool.uv]
package = false
```

#### Step 3 — FastAPI app

```python
# apis/my-api/src/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from core.logging import get_logger

logger = get_logger(__name__)
app = FastAPI(title="My API", version="0.1.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
```

#### Step 4 — Dockerfile

```dockerfile
# apis/my-api/Dockerfile
FROM python:3.12-slim
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages/core  ./packages/core
COPY apis/my-api    ./apis/my-api

RUN pip install uv && \
    uv sync --package my-api --frozen --no-dev

EXPOSE 8000
CMD ["uv", "run", "--package", "my-api", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

> Only copy the packages your API depends on — keep the image small.

#### Step 5 — Run locally

```bash
uv sync
uv run --package my-api uvicorn src.main:app --reload
```

Visit `http://localhost:8000/docs`

#### Step 6 — Build and run with Docker

```bash
docker build -f apis/my-api/Dockerfile -t my-api .
docker run -p 8000:8000 --env-file .env my-api
```

### Deploy APIs

Add a GitHub Actions workflow (see `deploy-gateway.yml` as the template):

```yaml
# .github/workflows/deploy-my-api.yml
name: Deploy my-api

on:
  push:
    branches: [main]
    paths:
      - 'apis/my-api/**'
      - 'packages/core/**'
      - 'uv.lock'

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - name: Build image
        run: docker build -f apis/my-api/Dockerfile -t my-api .
      # Add your deploy step here (Fly.io, Railway, Render, etc.)
```

Cloud options: **Fly.io**, **Railway**, **Render**, **Google Cloud Run** — all accept a Docker image.

---

## 5. MCP Servers

MCP servers live in `mcp-servers/`. They expose **tools** and **resources** to Claude (via Claude Desktop or Claude Code) using the [Model Context Protocol](https://modelcontextprotocol.io).

**When to use:** give Claude access to custom tools — search your database, run code, read files, call internal APIs.

### Existing MCP servers

| Package | Tools / Resources |
|---|---|
| `knowledge-base` | `search_knowledge_base(query)` tool, `kb://status` resource |
| `code-runner` | `run_python(code, timeout)` tool — executes Python in a subprocess |

### Run existing MCP servers

```bash
# knowledge-base (stdio mode — for Claude Desktop / Claude Code)
uv run --package knowledge-base python src/server.py

# code-runner
uv run --package code-runner python src/server.py
```

### Wire into Claude Desktop or Claude Code

Add to your MCP config (`~/.claude/mcp_servers.json` or Claude Desktop settings):

```json
{
  "mcpServers": {
    "knowledge-base": {
      "command": "uv",
      "args": ["run", "--package", "knowledge-base", "python", "src/server.py"],
      "cwd": "/path/to/python-turborepo"
    },
    "code-runner": {
      "command": "uv",
      "args": ["run", "--package", "code-runner", "python", "src/server.py"],
      "cwd": "/path/to/python-turborepo"
    }
  }
}
```

Claude will now have access to those tools in every conversation.

### Create a new MCP server

#### Step 1 — Scaffold

```bash
mkdir -p mcp-servers/my-server/src
touch mcp-servers/my-server/src/__init__.py
```

#### Step 2 — pyproject.toml

```toml
# mcp-servers/my-server/pyproject.toml
[project]
name = "my-server"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastmcp>=2.0",
    "core",
]

[tool.uv.sources]
core = { workspace = true }

[tool.uv]
package = false
```

#### Step 3 — server.py

```python
# mcp-servers/my-server/src/server.py
from fastmcp import FastMCP
from core.logging import get_logger

logger = get_logger(__name__)
mcp = FastMCP("my-server")


@mcp.tool()
def my_tool(input: str) -> str:
    """Describe what this tool does — Claude reads this docstring."""
    logger.info("my_tool called with: %s", input)
    return f"Result for: {input}"


@mcp.tool()
def another_tool(query: str, limit: int = 5) -> list[str]:
    """Return up to `limit` results for the given query."""
    return [f"result_{i}" for i in range(limit)]


@mcp.resource("myserver://status")
def status() -> str:
    """Resource: exposes static or dynamic data to Claude."""
    return "my-server is running"


if __name__ == "__main__":
    mcp.run()
```

**Tools** = functions Claude can call actively (actions).
**Resources** = data Claude can read (like files or DB records).

#### Step 4 — Run it

```bash
uv sync
uv run --package my-server python src/server.py
```

#### Step 5 — Register with Claude

```json
{
  "mcpServers": {
    "my-server": {
      "command": "uv",
      "args": ["run", "--package", "my-server", "python", "src/server.py"],
      "cwd": "/path/to/python-turborepo"
    }
  }
}
```

### Deploy MCP servers

MCP servers run as **stdio processes** locally (the default). For remote/team use, you can run them over HTTP using FastMCP's SSE mode:

```python
# Run over HTTP (SSE transport) instead of stdio
if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=8000)
```

```dockerfile
# mcp-servers/my-server/Dockerfile
FROM python:3.12-slim
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages/core          ./packages/core
COPY mcp-servers/my-server  ./mcp-servers/my-server

RUN pip install uv && \
    uv sync --package my-server --frozen --no-dev

EXPOSE 8000
CMD ["uv", "run", "--package", "my-server", "python", "src/server.py"]
```

---

## 6. Shared Packages

Packages live in `packages/`. They are real Python libraries imported by agents, APIs, and MCP servers.

| Package | What it provides |
|---|---|
| `core` | `Settings` (reads `.env`), `get_logger()`, shared types |
| `llm` | Anthropic client, `stream_text()` streaming helper |
| `tools` | `web_search`, `file_ops`, `memory` tools for agents |

### Use a shared package

Add it to the importing package's dependencies:

```toml
dependencies = ["core", "llm"]

[tool.uv.sources]
core = { workspace = true }
llm  = { workspace = true }
```

Then import normally:

```python
from core.config import get_settings
from core.logging import get_logger
from llm.streaming import stream_text
```

### Add a new shared package

```bash
mkdir -p packages/my-lib/src/my_lib
touch packages/my-lib/src/my_lib/__init__.py
```

```toml
# packages/my-lib/pyproject.toml
[project]
name = "my-lib"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

> Shared packages keep `[build-system]` and do NOT set `package = false` — they are built as real wheels so other workspace members can import them.

```bash
uv sync   # picks up the new package automatically
```

---

## Quick Reference

| What | Command |
|---|---|
| Install all deps | `uv sync` |
| Run an app locally | `uv run --package hello-api uvicorn api.index:app --reload --app-dir apps/hello-api` |
| Run a research agent | `uv run --package research-agent python -m src.main "your query"` |
| Run a coding agent | `uv run --package coding-agent python -m src.main "your task"` |
| Run gateway API | `uv run --package gateway uvicorn src.main:app --reload` |
| Run an MCP server | `uv run --package knowledge-base python src/server.py` |
| Add a dep to a package | `uv add httpx --package gateway` |
| Build Docker image | `docker build -f apis/gateway/Dockerfile -t gateway .` |
| Run Docker container | `docker run -p 8000:8000 --env-file .env gateway` |
