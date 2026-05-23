import random
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(
    title="Hello API",
    description="A simple FastAPI app deployed on Vercel",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

QUOTES: list[dict] = [
    {"id": 1, "text": "Code is like humor. When you have to explain it, it's bad.", "author": "Cory House"},
    {"id": 2, "text": "First, solve the problem. Then, write the code.", "author": "John Johnson"},
    {"id": 3, "text": "Any fool can write code that a computer can understand. Good programmers write code that humans can understand.", "author": "Martin Fowler"},
    {"id": 4, "text": "Make it work, make it right, make it fast.", "author": "Kent Beck"},
    {"id": 5, "text": "The best code is no code at all.", "author": "Jeff Atwood"},
]


class Quote(BaseModel):
    id: int
    text: str
    author: str


class Service(BaseModel):
    name: str
    base: str
    docs: str
    endpoints: list[str]


class Registry(BaseModel):
    name: str
    version: str
    deployed_at: str
    services: list[Service]


SERVICES: list[Service] = [
    Service(
        name="Hello API",
        base="/",
        docs="/docs",
        endpoints=["/health", "/quotes", "/quotes/random", "/quotes/{id}", "/utils/uuid", "/utils/timestamp"],
    ),
    Service(
        name="Notes API",
        base="/notes",
        docs="/notes/docs",
        endpoints=["GET /notes", "POST /notes", "GET /notes/{id}", "PUT /notes/{id}", "DELETE /notes/{id}"],
    ),
    Service(
        name="Code Reviewer",
        base="/code-review",
        docs="/code-review/docs",
        endpoints=["POST /code-review", "GET /code-review/health"],
    ),
]


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    cards = ""
    for s in SERVICES:
        endpoints = "".join(f"<li><code>{e}</code></li>" for e in s.endpoints)
        cards += f"""
        <div class="card">
            <div class="card-header">
                <h2>{s.name}</h2>
                <a href="{s.docs}" class="docs-btn">Docs ↗</a>
            </div>
            <p class="base">Base: <code>{s.base}</code></p>
            <ul>{endpoints}</ul>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bifrost API</title>
    <script defer src="/_vercel/speed-insights/script.js"></script>
    <script defer src="/_vercel/insights/script.js"></script>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0a0a0a; color: #e5e5e5; min-height: 100vh; padding: 48px 24px; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        header {{ margin-bottom: 48px; }}
        h1 {{ font-size: 2.5rem; font-weight: 700; background: linear-gradient(135deg, #6366f1, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .meta {{ margin-top: 8px; color: #737373; font-size: 0.9rem; }}
        .meta span {{ margin-right: 16px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px; }}
        .card {{ background: #141414; border: 1px solid #262626; border-radius: 12px; padding: 24px; transition: border-color 0.2s; }}
        .card:hover {{ border-color: #6366f1; }}
        .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
        .card-header h2 {{ font-size: 1.1rem; font-weight: 600; }}
        .docs-btn {{ font-size: 0.8rem; color: #6366f1; text-decoration: none; border: 1px solid #6366f1; padding: 4px 10px; border-radius: 6px; }}
        .docs-btn:hover {{ background: #6366f1; color: white; }}
        .base {{ font-size: 0.85rem; color: #737373; margin-bottom: 12px; }}
        ul {{ list-style: none; display: flex; flex-direction: column; gap: 6px; }}
        li code {{ font-size: 0.8rem; background: #1f1f1f; padding: 4px 8px; border-radius: 4px; color: #a78bfa; }}
        footer {{ margin-top: 48px; text-align: center; color: #525252; font-size: 0.85rem; }}
        footer a {{ color: #6366f1; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>⚡ Bifrost API</h1>
            <div class="meta">
                <span>v0.3.0</span>
                <span><a href="https://github.com/nikharmsingh/bifrost" style="color:#6366f1">GitHub ↗</a></span>
            </div>
        </header>
        <div class="grid">{cards}</div>
        <footer>
            <p>Built with FastAPI · Deployed on <a href="https://vercel.com">Vercel</a></p>
        </footer>
    </div>
</body>
</html>"""


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/quotes", response_model=list[Quote])
async def list_quotes() -> list[dict]:
    return QUOTES


@app.get("/quotes/random", response_model=Quote)
async def random_quote() -> dict:
    return random.choice(QUOTES)


@app.get("/quotes/{quote_id}", response_model=Quote)
async def get_quote(quote_id: int) -> dict:
    match = next((q for q in QUOTES if q["id"] == quote_id), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Quote {quote_id} not found")
    return match


@app.get("/utils/uuid")
async def generate_uuid() -> dict:
    return {"uuid": str(uuid.uuid4())}


@app.get("/utils/timestamp")
async def timestamp() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "iso": now.isoformat(),
        "unix": int(now.timestamp()),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
    }
