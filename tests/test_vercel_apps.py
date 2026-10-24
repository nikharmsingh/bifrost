import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from conftest import load_module

hello = load_module("hello_api", "apps/hello-api/api/index.py")
code_reviewer = load_module("code_reviewer", "apps/code-reviewer/api/index.py")
resume = load_module("resume_analyzer", "apps/resume-analyzer/api/index.py")
notes = load_module("notes_api", "apps/notes-api/api/index.py")


# ── hello-api ──────────────────────────────────────────────────────────────────


def test_hello_health_and_quotes():
    client = TestClient(hello.app)
    assert client.get("/health").status_code == 200
    quotes = client.get("/quotes").json()
    assert len(quotes) == len(hello.QUOTES)
    assert client.get("/quotes/999").status_code == 404


# ── code-reviewer ──────────────────────────────────────────────────────────────


def test_code_review_rejects_empty_and_oversized_code(monkeypatch):
    monkeypatch.setitem(code_reviewer.app.dependency_overrides, code_reviewer.auth.require_key("groq"), lambda: "gsk_x")
    client = TestClient(code_reviewer.app)
    assert client.post("/code-review", json={"code": "   "}).status_code == 400
    assert client.post("/code-review", json={"code": "x" * 10_001}).status_code == 400


# ── resume-analyzer ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/job",
        "http://127.0.0.1/admin",
        "http://localhost:8000",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]/",
    ],
)
def test_assert_public_url_blocks_internal_targets(url):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(resume.assert_public_url(url))
    assert exc.value.status_code == 422


def test_analyze_rejects_internal_job_url(monkeypatch):
    monkeypatch.setitem(resume.app.dependency_overrides, resume.auth.require_key("groq"), lambda: "gsk_x")
    client = TestClient(resume.app)
    response = client.post(
        "/resume-analyzer/analyze",
        files={"resume": ("cv.pdf", b"%PDF-1.4", "application/pdf")},
        data={"job_url": "http://127.0.0.1/"},
    )
    # Fails at PDF parsing or the SSRF guard — either way no request is made to the internal host
    assert response.status_code == 422


def test_ui_does_not_render_model_output_as_html():
    html = TestClient(resume.app).get("/resume-analyzer").text
    assert ".innerHTML =" not in html


# ── notes-api ──────────────────────────────────────────────────────────────────


def test_notes_without_database_returns_503(monkeypatch):
    import bifrost_auth

    monkeypatch.setattr(notes, "engine", None)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    # A signed-in request reaches the user lookup, which reports the missing database instead of crashing
    session = bifrost_auth.sign("some-user-id")
    client = TestClient(notes.app, base_url="https://testserver", cookies={bifrost_auth.SESSION_COOKIE: session})
    assert client.get("/notes").status_code == 503
    assert TestClient(notes.app).get("/notes/health").status_code == 503


# ── app UIs ────────────────────────────────────────────────────────────────────


def test_code_reviewer_serves_ui():
    response = TestClient(code_reviewer.app).get("/code-review")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<title>Code Reviewer</title>" in response.text
    assert ".innerHTML =" not in response.text


def test_notes_ui_is_not_captured_as_note_id(monkeypatch):
    monkeypatch.setattr(notes, "engine", None)
    # Served without touching the database, and not routed to GET /notes/{note_id}
    response = TestClient(notes.app).get("/notes/ui")
    assert response.status_code == 200
    assert "<title>Notes</title>" in response.text
    assert ".innerHTML =" not in response.text


def test_code_review_returns_502_when_groq_model_fails(monkeypatch):
    import httpx
    from groq import APIError

    class FailingGroq:
        def __init__(self, api_key):
            self.chat = self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            assert kwargs["model"] == code_reviewer.GROQ_MODEL
            raise APIError("model_not_found", request=httpx.Request("POST", "https://api.groq.com"), body=None)

    monkeypatch.setattr(code_reviewer, "Groq", FailingGroq)
    monkeypatch.setitem(code_reviewer.app.dependency_overrides, code_reviewer.auth.require_key("groq"), lambda: "gsk_x")
    response = TestClient(code_reviewer.app).post("/code-review", json={"code": "print(1)"})
    assert response.status_code == 502
    assert "model_not_found" in response.json()["detail"]


# ── edge caching ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("module", "path"),
    [
        ("hello", "/"),
        ("hello", "/settings"),
        ("code_reviewer", "/code-review"),
        ("resume", "/resume-analyzer"),
        ("notes", "/notes/ui"),
    ],
)
def test_ui_pages_are_edge_cached_but_browsers_revalidate(module, path):
    response = TestClient(globals()[module].app).get(path)
    assert response.status_code == 200
    assert "max-age=300" in response.headers["vercel-cdn-cache-control"]
    assert response.headers["cache-control"] == "public, max-age=0, must-revalidate"
    # A cached page must never carry a cookie or vary per visitor
    assert "set-cookie" not in response.headers


def test_personal_endpoints_are_never_edge_cached(monkeypatch):
    monkeypatch.setattr(notes, "engine", None)
    for path in ("/notes", "/notes/me"):
        assert "vercel-cdn-cache-control" not in TestClient(notes.app).get(path).headers
