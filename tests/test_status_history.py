"""Uptime history: recording checks behind a secret, retention, and the 24 h / 7 d report."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from conftest import load_module

hello = load_module("hello_api_status", "apps/hello-api/api/index.py")
NOW = datetime(2026, 10, 25, 12, 30, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch):
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    hello.status_metadata.create_all(engine)
    monkeypatch.setattr(hello, "engine", engine)
    return engine


def mock_apps(down: set[str]):
    """httpx client whose app health endpoints answer 200, or 503 for prefixes in `down`."""

    def handler(request: httpx.Request) -> httpx.Response:
        prefix = request.url.path.removesuffix("/health")
        return httpx.Response(503 if prefix in down else 200)

    return lambda base: httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(handler))


def insert(db, when: datetime, slug: str, ok: bool, latency: int | None = 100):
    with db.begin() as conn:
        conn.execute(
            sa.insert(hello.health_checks_table).values(
                checked_at=when.isoformat(), slug=slug, ok=ok, status_code=200 if ok else 503, latency_ms=latency
            )
        )


def test_check_endpoint_requires_the_secret(db, monkeypatch):
    client = TestClient(hello.app)
    monkeypatch.delenv("STATUS_CHECK_SECRET", raising=False)
    assert client.post("/status/check").status_code == 503
    monkeypatch.setenv("STATUS_CHECK_SECRET", "s3cret")
    assert client.post("/status/check").status_code == 401
    assert client.post("/status/check", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_check_records_every_app_and_reports_down(db, monkeypatch):
    monkeypatch.setenv("STATUS_CHECK_SECRET", "s3cret")
    monkeypatch.setattr(hello, "http_client", mock_apps(down={"/notes"}))
    response = TestClient(hello.app).post("/status/check", headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200
    assert response.json() == {"recorded": 3, "down": ["notes-api"]}
    with db.connect() as conn:
        rows = conn.execute(sa.select(hello.health_checks_table)).mappings().all()
    assert {r["slug"]: r["ok"] for r in rows} == {"code-reviewer": True, "notes-api": False, "resume-analyzer": True}


def test_old_checks_are_pruned(db, monkeypatch):
    monkeypatch.setattr(hello, "http_client", mock_apps(down=set()))
    insert(db, NOW - timedelta(days=9), "notes-api", True)
    insert(db, NOW - timedelta(days=2), "notes-api", True)
    asyncio.run(hello.record_health_checks("https://bifrost.test", now=NOW))
    with db.connect() as conn:
        times = [r for (r,) in conn.execute(sa.select(hello.health_checks_table.c.checked_at))]
    assert (NOW - timedelta(days=9)).isoformat() not in times
    assert (NOW - timedelta(days=2)).isoformat() in times


def test_report_computes_uptime_latency_and_hourly_buckets(db):
    # Last 24 h for notes-api: 3 up (100/200/300 ms) and 1 down in the current hour
    for minutes, ok, latency in [(5, True, 100), (15, True, 200), (25, True, 300), (28, False, None)]:
        insert(db, NOW.replace(minute=0) + timedelta(minutes=minutes), "notes-api", ok, latency)
    # Older than 24 h but inside 7 d: counts only toward the 7-day figure
    insert(db, NOW - timedelta(days=3), "notes-api", False, None)

    report = hello.build_status_report(now=NOW)
    notes = next(a for a in report.apps if a.slug == "notes-api")
    assert notes.uptime_24h == 75.0
    assert notes.uptime_7d == 60.0
    assert notes.avg_latency_24h_ms == 200  # failed checks don't drag latency down
    assert notes.last_ok is False
    assert len(notes.hours) == 24
    current = notes.hours[-1]
    assert (current.checks, current.ok, current.avg_latency_ms) == (4, 3, 200)
    assert all(h.checks == 0 for h in notes.hours[:-1])

    untouched = next(a for a in report.apps if a.slug == "code-reviewer")
    assert untouched.uptime_24h is None and untouched.last_checked_at is None


def test_status_endpoint_is_edge_cached(db):
    response = TestClient(hello.app).get("/status")
    assert response.status_code == 200
    assert {a["slug"] for a in response.json()["apps"]} == {"code-reviewer", "notes-api", "resume-analyzer"}
    assert "max-age=60" in response.headers["vercel-cdn-cache-control"]
