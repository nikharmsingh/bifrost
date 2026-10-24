import asyncio
import json
import os
import pathlib
import random
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy.pool import NullPool

ROOT_DIR = pathlib.Path(__file__).resolve().parents[3]

# Vercel installs from requirements.txt, not the uv workspace, so load the shared auth package from the repo
sys.path.insert(0, str(ROOT_DIR / "packages" / "auth" / "src"))
from bifrost_auth import Auth  # noqa: E402

DATABASE_URL = os.getenv("DATABASE_URL", "")
# NullPool is required for serverless — Neon handles pooling via PgBouncer
engine = sa.create_engine(DATABASE_URL, poolclass=NullPool) if DATABASE_URL else None


def require_db() -> sa.Engine:
    if not engine:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return engine


auth = Auth(require_db)


def _read_version() -> str:
    try:
        return (ROOT_DIR / "VERSION").read_text().strip()
    except FileNotFoundError:
        return "dev"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if engine:
        auth.create_tables()
    yield


app = FastAPI(
    title="Hello API",
    description="Bifrost dashboard, shared GitHub sign-in (/auth), plus quotes and utility endpoints.",
    version=_read_version(),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# One sign-in for every Bifrost app: /auth/login, /auth/callback, /auth/logout, /auth/me
app.include_router(auth.router())

QUOTES: list[dict] = [
    {"id": 1, "text": "Code is like humor. When you have to explain it, it's bad.", "author": "Cory House"},
    {"id": 2, "text": "First, solve the problem. Then, write the code.", "author": "John Johnson"},
    {
        "id": 3,
        "text": "Any fool can write code that a computer can understand. Good programmers write code that humans can understand.",
        "author": "Martin Fowler",
    },
    {"id": 4, "text": "Make it work, make it right, make it fast.", "author": "Kent Beck"},
    {"id": 5, "text": "The best code is no code at all.", "author": "Jeff Atwood"},
]


class Quote(BaseModel):
    id: int
    text: str
    author: str


class Endpoint(BaseModel):
    method: str
    path: str
    summary: str
    auth: bool


class Health(BaseModel):
    ok: bool
    status_code: int | None
    latency_ms: int | None


class AppInfo(BaseModel):
    slug: str
    name: str
    description: str
    version: str
    prefix: str
    docs: str
    ui: str | None
    health: Health
    endpoints: list[Endpoint]


class Release(BaseModel):
    tag: str
    name: str
    url: str
    published_at: str


class Registry(BaseModel):
    name: str
    version: str
    generated_at: str
    release: Release | None
    apps: list[AppInfo]


# ── dashboard: app discovery ───────────────────────────────────────────────────

REPO = "nikharmsingh/bifrost"
REGISTRY_TTL_SECONDS = 30
RELEASE_TTL_SECONDS = 600
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

# Used only if vercel.json isn't bundled with the function
FALLBACK_ROUTES = [
    {"src": "/code-review(.*)", "dest": "apps/code-reviewer/api/index.py"},
    {"src": "/notes(.*)", "dest": "apps/notes-api/api/index.py"},
    {"src": "/resume-analyzer(.*)", "dest": "apps/resume-analyzer/api/index.py"},
    {"src": "/(.*)", "dest": "apps/hello-api/api/index.py"},
]

_registry_cache: dict[str, tuple[float, Registry]] = {}
_release_cache: tuple[float, Release | None] | None = None


def load_routes() -> list[tuple[str, str]]:
    """Return (slug, prefix) for every app routed in vercel.json — the single source of truth for what's deployed."""
    try:
        routes = json.loads((ROOT_DIR / "vercel.json").read_text())["routes"]
    except (OSError, KeyError, ValueError):
        routes = FALLBACK_ROUTES
    apps = []
    for route in routes:
        match = re.fullmatch(r"(/[\w-]*)\(\.\*\)", route.get("src", ""))
        parts = route.get("dest", "").split("/")
        prefix = match.group(1).rstrip("/") if match else ""
        # Skip the root catch-all — that's this app, which hosts the dashboard rather than being one of the apps
        if prefix and len(parts) > 2 and parts[0] == "apps":
            apps.append((parts[1], prefix))
    return apps


def parse_endpoints(spec: dict) -> list[Endpoint]:
    endpoints = []
    for path, operations in spec.get("paths", {}).items():
        for method, op in operations.items():
            if method not in HTTP_METHODS:
                continue
            # Declared security schemes (bearer token, session cookie) or a legacy X-API-Key header
            auth = bool(op.get("security")) or any(
                p.get("in") == "header" and p.get("name", "").lower() == "x-api-key" for p in op.get("parameters", [])
            )
            endpoints.append(Endpoint(method=method.upper(), path=path, summary=op.get("summary", ""), auth=auth))
    return endpoints


def find_ui_path(spec: dict, prefix: str) -> str | None:
    """The app's own HTML page — the shortest GET route under its prefix that returns text/html."""
    pages = [
        path
        for path, operations in spec.get("paths", {}).items()
        if (path == prefix or path.startswith(f"{prefix}/"))
        and "text/html" in operations.get("get", {}).get("responses", {}).get("200", {}).get("content", {})
    ]
    return min(pages, key=len) if pages else None


def http_client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=5, follow_redirects=False)


async def check_health(client: httpx.AsyncClient, prefix: str) -> Health:
    started = time.perf_counter()
    try:
        response = await client.get(f"{prefix}/health")
    except httpx.HTTPError:
        return Health(ok=False, status_code=None, latency_ms=None)
    latency = int((time.perf_counter() - started) * 1000)
    return Health(ok=response.status_code == 200, status_code=response.status_code, latency_ms=latency)


async def fetch_app(client: httpx.AsyncClient, slug: str, prefix: str) -> AppInfo:
    async def fetch_spec() -> dict:
        try:
            response = await client.get(f"{prefix}/openapi.json")
            return response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {}

    spec, health = await asyncio.gather(fetch_spec(), check_health(client, prefix))
    info = spec.get("info", {})
    return AppInfo(
        slug=slug,
        name=info.get("title", slug),
        description=info.get("description", ""),
        version=info.get("version", ""),
        prefix=prefix,
        docs=f"{prefix}/docs",
        ui=find_ui_path(spec, prefix),
        health=health,
        endpoints=parse_endpoints(spec),
    )


async def fetch_latest_release() -> Release | None:
    global _release_cache
    if _release_cache and _release_cache[0] > time.monotonic():
        return _release_cache[1]
    release = None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"https://api.github.com/repos/{REPO}/releases/latest")
        if response.status_code == 200:
            data = response.json()
            release = Release(
                tag=data["tag_name"],
                name=data.get("name") or data["tag_name"],
                url=data["html_url"],
                published_at=data.get("published_at") or "",
            )
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    _release_cache = (time.monotonic() + RELEASE_TTL_SECONDS, release)
    return release


def resolve_base_url(request: Request) -> str:
    if override := os.getenv("BIFROST_BASE_URL"):
        return override.rstrip("/")
    # Vercel terminates TLS at the edge, so trust its forwarded scheme
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{request.url.netloc}"


async def build_registry(base_url: str) -> Registry:
    cached = _registry_cache.get(base_url)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    async with http_client(base_url) as client:
        apps, release = await asyncio.gather(
            asyncio.gather(*(fetch_app(client, slug, prefix) for slug, prefix in load_routes())),
            fetch_latest_release(),
        )
    registry = Registry(
        name="Bifrost",
        version=app.version,
        generated_at=datetime.now(UTC).isoformat(),
        release=release,
        apps=sorted(apps, key=lambda a: a.name.lower()),
    )
    _registry_cache[base_url] = (time.monotonic() + REGISTRY_TTL_SECONDS, registry)
    return registry


def _load_dashboard_html() -> str:
    try:
        return (pathlib.Path(__file__).parent / "dashboard.html").read_text()
    except FileNotFoundError:
        return (
            "<!DOCTYPE html><title>Bifrost</title><p>Dashboard unavailable — see <a href='/registry'>/registry</a>.</p>"
        )


DASHBOARD_HTML = _load_dashboard_html()


def _load_page(name: str) -> str:
    try:
        return (pathlib.Path(__file__).parent / name).read_text()
    except FileNotFoundError:
        return "<!DOCTYPE html><title>Bifrost</title><p>Page unavailable — <a href='/'>back to the dashboard</a>.</p>"


SETTINGS_HTML = _load_page("settings.html")


# Same for every visitor (personal data loads via JS), so serve it from Vercel's regional edge cache.
# Browsers always revalidate; the edge refreshes in the background.
PAGE_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=0, must-revalidate",
    "Vercel-CDN-Cache-Control": "max-age=300, stale-while-revalidate=86400",
}
# Public, slow-changing data: short edge TTL so health stays current
REGISTRY_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=0, must-revalidate",
    "Vercel-CDN-Cache-Control": "max-age=30, stale-while-revalidate=300",
}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(response: Response) -> str:
    response.headers.update(PAGE_CACHE_HEADERS)
    return DASHBOARD_HTML.replace("{{version}}", app.version)


@app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(response: Response) -> str:
    response.headers.update(PAGE_CACHE_HEADERS)
    return SETTINGS_HTML


@app.get("/registry", response_model=Registry, summary="Live registry of deployed apps")
async def registry(request: Request, response: Response) -> Registry:
    response.headers.update(REGISTRY_CACHE_HEADERS)
    return await build_registry(resolve_base_url(request))


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}


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
    now = datetime.now(UTC)
    return {
        "iso": now.isoformat(),
        "unix": int(now.timestamp()),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
    }
