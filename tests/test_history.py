"""Saved history, share links and usage for the AI apps (shared ai_runs table)."""

from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

import bifrost_auth
from bifrost_auth import Auth, runs
from conftest import load_module

code_reviewer = load_module("code_reviewer_history", "apps/code-reviewer/api/index.py")
resume = load_module("resume_analyzer_history", "apps/resume-analyzer/api/index.py")

ALICE = {"id": 1, "login": "alice", "name": None, "avatar_url": None}
BOB = {"id": 2, "login": "bob", "name": None, "avatar_url": None}
REVIEW_JSON = '{"summary": "fine", "score": 7, "issues": [], "strengths": ["short"], "recommendation": "ok"}'


class FakeGroq:
    """groq.Groq stand-in that answers with REVIEW_JSON and reports token usage."""

    content = REVIEW_JSON

    def __init__(self, api_key):
        self.chat = self.completions = self

    def create(self, **kwargs):
        class Usage:
            prompt_tokens, completion_tokens = 120, 30

        class Message:
            content = FakeGroq.content

        class Choice:
            message = Message()

        class Completion:
            choices = [Choice()]
            usage = Usage()

        return Completion()


@pytest.fixture
def db(monkeypatch):
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    bifrost_auth.metadata.create_all(engine)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    for module in (code_reviewer, resume):
        monkeypatch.setattr(module, "engine", engine)
        monkeypatch.setattr(module, "Groq", FakeGroq)
        monkeypatch.setitem(module.app.dependency_overrides, module.auth.require_key("groq"), lambda: "gsk_user")
    return engine


def client(app: FastAPI, profile: dict | None) -> TestClient:
    cookies = {}
    if profile:
        user = code_reviewer.auth.upsert_github_user(profile)
        cookies = {bifrost_auth.SESSION_COOKIE: bifrost_auth.sign(user["id"])}
    return TestClient(app, base_url="https://testserver", cookies=cookies)


def review(c: TestClient, code: str = "def add(a, b):\n    return a - b") -> dict:
    response = c.post("/code-review", json={"code": code, "language": "python"})
    assert response.status_code == 200
    return response.json()


def test_review_is_saved_to_history_with_its_input(db):
    alice = client(code_reviewer.app, ALICE)
    run_id = review(alice)["run_id"]
    assert run_id

    [item] = alice.get("/code-review/history").json()
    assert item == {
        "id": run_id,
        "title": "python · def add(a, b):",
        "score": 7,
        "created_at": item["created_at"],
        "shared": False,
    }
    full = alice.get(f"/code-review/history/{run_id}").json()
    assert full["input"] == {"code": "def add(a, b):\n    return a - b", "language": "python"}
    assert full["result"]["recommendation"] == "ok" and "run_id" not in full["result"]


def test_history_is_private_and_per_app(db):
    run_id = review(client(code_reviewer.app, ALICE))["run_id"]
    bob = client(code_reviewer.app, BOB)
    assert bob.get("/code-review/history").json() == []
    for request in (bob.get, bob.delete):
        assert request(f"/code-review/history/{run_id}").status_code == 404
    assert bob.post(f"/code-review/history/{run_id}/share").status_code == 404
    # A Code Reviewer run isn't visible through Resume Analyzer's history either
    assert client(resume.app, ALICE).get(f"/resume-analyzer/history/{run_id}").status_code == 404
    assert client(code_reviewer.app, None).get("/code-review/history").status_code == 401


def test_share_link_is_public_until_revoked(db):
    alice = client(code_reviewer.app, ALICE)
    run_id = review(alice)["run_id"]

    link = alice.post(f"/code-review/history/{run_id}/share").json()
    assert link["share_path"] == f"/code-review?shared={link['share_id']}"
    assert len(link["share_id"]) >= 20
    # Sharing again returns the same link
    assert alice.post(f"/code-review/history/{run_id}/share").json() == link
    assert alice.get("/code-review/history").json()[0]["shared"] is True

    anonymous = client(code_reviewer.app, None)
    shared = anonymous.get(f"/code-review/shared/{link['share_id']}")
    assert shared.status_code == 200
    assert shared.json()["result"]["score"] == 7 and "user_id" not in shared.text
    assert anonymous.get("/code-review/shared/not-a-real-id").status_code == 404

    assert alice.delete(f"/code-review/history/{run_id}/share").status_code == 204
    assert anonymous.get(f"/code-review/shared/{link['share_id']}").status_code == 404


def test_deleting_erases_content_but_keeps_usage(db):
    alice = client(code_reviewer.app, ALICE)
    run_id = review(alice, "SECRET_CODE = 1")["run_id"]
    share_id = alice.post(f"/code-review/history/{run_id}/share").json()["share_id"]

    assert alice.delete(f"/code-review/history/{run_id}").status_code == 204
    assert alice.get("/code-review/history").json() == []
    assert alice.get(f"/code-review/history/{run_id}").status_code == 404
    assert client(code_reviewer.app, None).get(f"/code-review/shared/{share_id}").status_code == 404
    with db.connect() as conn:
        row = conn.execute(sa.select(bifrost_auth.runs_table)).mappings().one()
    assert "SECRET_CODE" not in row["input"] + row["result"] + row["title"]
    assert (row["prompt_tokens"], row["completion_tokens"]) == (120, 30)


def test_review_still_returns_when_history_cannot_be_saved(db, monkeypatch):
    def broken(*args, **kwargs):
        raise sa.exc.OperationalError("insert", {}, Exception("db down"))

    monkeypatch.setattr(code_reviewer.runs, "record", broken)
    body = review(client(code_reviewer.app, ALICE))
    assert body["score"] == 7 and body["run_id"] is None


def test_resume_analysis_is_saved_without_the_resume(db, monkeypatch):
    monkeypatch.setattr(
        FakeGroq,
        "content",
        '{"match_score": 81, "verdict": "strong", "strengths": [], "gaps": [], "suggestions": [],'
        ' "keywords_matched": ["python"], "keywords_missing": []}',
    )

    async def fake_fetch(url):
        return "Senior Engineer — Acme", "We need Python."

    monkeypatch.setattr(resume, "fetch_job_posting", fake_fetch)
    monkeypatch.setattr(resume, "extract_pdf_text", lambda data: "RESUME_TEXT Python developer")

    alice = client(resume.app, ALICE)
    response = alice.post(
        "/resume-analyzer/analyze",
        files={"resume": ("cv.pdf", b"%PDF-1.4", "application/pdf")},
        data={"job_url": "https://jobs.example/123"},
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    [item] = alice.get("/resume-analyzer/history").json()
    assert (item["title"], item["score"]) == ("Senior Engineer — Acme", 81)
    full = alice.get(f"/resume-analyzer/history/{run_id}").json()
    assert full["input"] == {
        "job_url": "https://jobs.example/123",
        "job_title": "Senior Engineer — Acme",
        "file_name": "cv.pdf",
    }
    with db.connect() as conn:
        stored = conn.execute(sa.select(bifrost_auth.runs_table.c.input, bifrost_auth.runs_table.c.result)).one()
    assert "RESUME_TEXT" not in "".join(stored)

    # A share link shows the job and analysis but not the file name
    share_id = alice.post(f"/resume-analyzer/history/{run_id}/share").json()["share_id"]
    shared = client(resume.app, None).get(f"/resume-analyzer/shared/{share_id}").json()
    assert shared["input"] == {"job_url": "https://jobs.example/123", "job_title": "Senior Engineer — Acme"}
    assert shared["result"]["match_score"] == 81


# ── usage ──────────────────────────────────────────────────────────────────────


def insert_run(db, user_id: str, app: str, when: datetime, tokens=(100, 20)):
    with db.begin() as conn:
        conn.execute(
            sa.insert(bifrost_auth.runs_table).values(
                id=f"{app}-{when.isoformat()}",
                user_id=user_id,
                app=app,
                title="t",
                input="{}",
                result="{}",
                prompt_tokens=tokens[0],
                completion_tokens=tokens[1],
                created_at=when.isoformat(),
            )
        )


def test_usage_totals_per_day_and_app(db):
    insert_run(db, "u1", "code-review", datetime(2026, 10, 25, 9, tzinfo=UTC))
    insert_run(db, "u1", "code-review", datetime(2026, 10, 25, 18, tzinfo=UTC))
    insert_run(db, "u1", "resume-analyzer", datetime(2026, 10, 20, tzinfo=UTC), tokens=(None, None))
    insert_run(db, "u1", "code-review", datetime(2026, 9, 1, tzinfo=UTC))  # outside the 30-day window
    insert_run(db, "u2", "code-review", datetime(2026, 10, 25, tzinfo=UTC))  # someone else

    with db.connect() as conn:
        report = runs.usage(conn, "u1", today=date(2026, 10, 25))
    assert len(report["days"]) == 30
    assert report["days"][0]["date"] == "2026-09-26" and report["days"][-1]["date"] == "2026-10-25"
    assert report["days"][-1] == {"date": "2026-10-25", "requests": 2, "prompt_tokens": 200, "completion_tokens": 40}
    assert report["total"] == {"requests": 3, "prompt_tokens": 200, "completion_tokens": 40}
    assert report["apps"] == [
        {"app": "code-review", "requests": 2, "prompt_tokens": 200, "completion_tokens": 40},
        {"app": "resume-analyzer", "requests": 1, "prompt_tokens": 0, "completion_tokens": 0},
    ]


def test_usage_endpoint_requires_sign_in(db):
    app = FastAPI()
    auth = Auth(lambda: db)
    app.include_router(auth.router())
    assert TestClient(app).get("/auth/usage").status_code == 401

    alice = client(code_reviewer.app, ALICE)
    review(alice)
    usage = TestClient(app, base_url="https://testserver", cookies=dict(alice.cookies)).get("/auth/usage").json()
    assert usage["total"] == {"requests": 1, "prompt_tokens": 120, "completion_tokens": 30}
