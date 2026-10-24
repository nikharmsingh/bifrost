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


def test_code_review_rejects_empty_and_oversized_code():
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


def test_analyze_rejects_internal_job_url():
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


def test_notes_write_requires_api_key_when_configured(monkeypatch):
    monkeypatch.setattr(notes, "NOTES_API_KEY", "secret")
    client = TestClient(notes.app)
    body = {"title": "t", "content": "c"}
    assert client.post("/notes", json=body).status_code == 401
    assert client.post("/notes", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.delete("/notes/abc").status_code == 401


def test_notes_without_database_returns_503(monkeypatch):
    monkeypatch.setattr(notes, "engine", None)
    monkeypatch.setattr(notes, "NOTES_API_KEY", "")
    client = TestClient(notes.app)
    assert client.get("/notes").status_code == 503
