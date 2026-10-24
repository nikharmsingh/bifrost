import asyncio
import hmac
import json
import os
import pathlib
import random
import re
import sys
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import sqlalchemy as sa
from fastapi import Depends, FastAPI, Header, HTTPException, Request
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
        status_metadata.create_all(engine)
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


# ── status history ─────────────────────────────────────────────────────────────
# GitHub Actions POSTs /status/check every 10 minutes (Vercel Hobby cron only runs daily); each run records
# every app's /health result, and GET /status turns the last 7 days into uptime and hourly buckets.

STATUS_RETENTION_DAYS = 8
status_metadata = sa.MetaData()
health_checks_table = sa.Table(
    "health_checks",
    status_metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("checked_at", sa.String, nullable=False, index=True),
    sa.Column("slug", sa.String, nullable=False),
    sa.Column("ok", sa.Boolean, nullable=False),
    sa.Column("status_code", sa.Integer),
    sa.Column("latency_ms", sa.Integer),
)


class HourBucket(BaseModel):
    hour: str
    checks: int
    ok: int
    avg_latency_ms: int | None


class AppStatus(BaseModel):
    slug: str
    uptime_24h: float | None
    uptime_7d: float | None
    avg_latency_24h_ms: int | None
    last_checked_at: str | None
    last_ok: bool | None
    hours: list[HourBucket]


class StatusReport(BaseModel):
    generated_at: str
    apps: list[AppStatus]


def require_status_secret(authorization: str = Header(default="")) -> None:
    secret = os.getenv("STATUS_CHECK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="Status checks are not configured (STATUS_CHECK_SECRET)")
    if not hmac.compare_digest(authorization, f"Bearer {secret}"):
        raise HTTPException(status_code=401, detail="Invalid status check secret")


async def record_health_checks(base_url: str, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    routes = load_routes()
    async with http_client(base_url) as client:
        results = await asyncio.gather(*(check_health(client, prefix) for _, prefix in routes))
    rows = [
        {"checked_at": now.isoformat(), "slug": slug, **result.model_dump()}
        for (slug, _), result in zip(routes, results, strict=True)
    ]
    cutoff = (now - timedelta(days=STATUS_RETENTION_DAYS)).isoformat()
    with require_db().begin() as conn:
        conn.execute(sa.insert(health_checks_table), rows)
        conn.execute(sa.delete(health_checks_table).where(health_checks_table.c.checked_at < cutoff))
    return rows


def _uptime(checks: list) -> float | None:
    return round(100 * sum(c["ok"] for c in checks) / len(checks), 2) if checks else None


def _avg_latency(checks: list) -> int | None:
    latencies = [c["latency_ms"] for c in checks if c["ok"] and c["latency_ms"] is not None]
    return round(sum(latencies) / len(latencies)) if latencies else None


def build_status_report(now: datetime | None = None) -> StatusReport:
    now = now or datetime.now(UTC)
    since_7d, since_24h = (now - timedelta(days=7)).isoformat(), (now - timedelta(hours=24)).isoformat()
    with require_db().connect() as conn:
        rows = conn.execute(
            sa.select(health_checks_table)
            .where(health_checks_table.c.checked_at >= since_7d)
            .order_by(health_checks_table.c.checked_at)
        ).mappings()
        by_slug: dict[str, list] = defaultdict(list)
        for row in rows:
            by_slug[row["slug"]].append(dict(row))

    this_hour = now.replace(minute=0, second=0, microsecond=0)
    hour_starts = [this_hour - timedelta(hours=23 - i) for i in range(24)]
    apps = []
    for slug, _ in load_routes():
        checks = by_slug.get(slug, [])
        recent = [c for c in checks if c["checked_at"] >= since_24h]
        per_hour: dict[datetime, list] = defaultdict(list)
        for c in recent:
            per_hour[datetime.fromisoformat(c["checked_at"]).replace(minute=0, second=0, microsecond=0)].append(c)
        apps.append(
            AppStatus(
                slug=slug,
                uptime_24h=_uptime(recent),
                uptime_7d=_uptime(checks),
                avg_latency_24h_ms=_avg_latency(recent),
                last_checked_at=checks[-1]["checked_at"] if checks else None,
                last_ok=checks[-1]["ok"] if checks else None,
                hours=[
                    HourBucket(
                        hour=start.isoformat(),
                        checks=len(per_hour[start]),
                        ok=sum(c["ok"] for c in per_hour[start]),
                        avg_latency_ms=_avg_latency(per_hour[start]),
                    )
                    for start in hour_starts
                ],
            )
        )
    return StatusReport(generated_at=now.isoformat(), apps=apps)


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


# History changes every 10 minutes at most
STATUS_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=0, must-revalidate",
    "Vercel-CDN-Cache-Control": "max-age=60, stale-while-revalidate=600",
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


@app.get("/status", response_model=StatusReport, summary="Uptime history (24 h and 7 d) for every app")
def status_report(response: Response) -> StatusReport:
    response.headers.update(STATUS_CACHE_HEADERS)
    return build_status_report()


@app.post(
    "/status/check",
    summary="Record a health check of every app (called by the scheduled workflow)",
    dependencies=[Depends(require_status_secret)],
)
async def run_status_check(request: Request) -> dict:
    rows = await record_health_checks(resolve_base_url(request))
    return {"recorded": len(rows), "down": [r["slug"] for r in rows if not r["ok"]]}


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
