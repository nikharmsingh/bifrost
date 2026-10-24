import asyncio

import httpx
from fastapi.testclient import TestClient

from conftest import load_module

hello = load_module("hello_api_dashboard", "apps/hello-api/api/index.py")
notes = load_module("notes_api_dashboard", "apps/notes-api/api/index.py")

SPECS = {
    "/code-review/openapi.json": {
        "info": {"title": "Code Reviewer", "description": "AI review", "version": "0.1.0"},
        "paths": {"/code-review": {"post": {"summary": "Review Code"}}},
    },
    "/notes/openapi.json": {
        "info": {"title": "Notes API", "version": "0.2.0"},
        "paths": {
            "/notes": {
                "get": {"summary": "List Notes"},
                "post": {"summary": "Create Note", "parameters": [{"in": "header", "name": "x-api-key"}]},
            },
            "/notes/ui": {"get": {"responses": {"200": {"content": {"text/html": {}}}}}},
        },
    },
    "/resume-analyzer/openapi.json": {
        "info": {"title": "Resume Analyzer", "version": "0.1.0"},
        "paths": {"/resume-analyzer": {"get": {"responses": {"200": {"content": {"text/html": {}}}}}}},
    },
}


def _mock_app(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path in SPECS:
        return httpx.Response(200, json=SPECS[path])
    if path == "/notes/health":
        return httpx.Response(503)
    if path.endswith("/health"):
        return httpx.Response(200, json={"status": "ok"})
    return httpx.Response(404)


def _registry(monkeypatch):
    monkeypatch.setattr(hello, "_registry_cache", {})
    monkeypatch.setattr(
        hello, "http_client", lambda base: httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(_mock_app))
    )

    async def no_release():
        return None

    monkeypatch.setattr(hello, "fetch_latest_release", no_release)
    return asyncio.run(hello.build_registry("https://bifrost.test"))


def test_routes_come_from_vercel_json_and_exclude_the_dashboard_host():
    routes = dict(hello.load_routes())
    assert routes == {"code-reviewer": "/code-review", "notes-api": "/notes", "resume-analyzer": "/resume-analyzer"}


def test_registry_reports_metadata_health_auth_and_ui(monkeypatch):
    apps = {a.slug: a for a in _registry(monkeypatch).apps}

    assert set(apps) == {"code-reviewer", "notes-api", "resume-analyzer"}
    assert apps["code-reviewer"].name == "Code Reviewer"
    assert apps["code-reviewer"].docs == "/code-review/docs"
    assert apps["code-reviewer"].health.ok

    notes_app = apps["notes-api"]
    assert not notes_app.health.ok and notes_app.health.status_code == 503
    assert {(e.method, e.path, e.auth) for e in notes_app.endpoints} >= {
        ("GET", "/notes", False),
        ("POST", "/notes", True),
    }

    assert apps["resume-analyzer"].ui == "/resume-analyzer"
    assert apps["notes-api"].ui == "/notes/ui"
    assert apps["code-reviewer"].ui is None


def test_dashboard_page_renders_version_without_unsafe_html():
    html = TestClient(hello.app).get("/").text
    assert f"v{hello.app.version}" in html
    assert "{{version}}" not in html
    assert ".innerHTML =" not in html


def test_notes_health_route_is_not_captured_as_note_id(monkeypatch):
    monkeypatch.setattr(notes, "engine", None)
    # 503 from require_db, not a 404 "Note not found" from GET /notes/{note_id}
    assert TestClient(notes.app).get("/notes/health").status_code == 503
