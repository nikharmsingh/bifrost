"""Bring-your-own Groq key: encryption at rest, the /auth/keys API, and that apps never use the server key."""

import base64
import secrets

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

import bifrost_auth
from bifrost_auth import Auth, keys
from conftest import load_module

code_reviewer = load_module("code_reviewer_byok", "apps/code-reviewer/api/index.py")
resume = load_module("resume_analyzer_byok", "apps/resume-analyzer/api/index.py")

USER_KEY = "gsk_user_" + "a" * 40
PROFILE = {"id": 7, "login": "octo", "name": None, "avatar_url": None}


@pytest.fixture
def engine(monkeypatch):
    db = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    bifrost_auth.metadata.create_all(db)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("KEY_ENCRYPTION_SECRET", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
    # The server's own key must never be used for visitors
    monkeypatch.setenv("GROQ_API_KEY", "gsk_SERVER_KEY_MUST_NOT_BE_USED")
    monkeypatch.setattr(keys, "validate_with_provider", lambda provider, api_key: None)
    for module in (code_reviewer, resume):
        monkeypatch.setattr(module, "engine", db)
    return db


def session_for(auth: Auth, profile: dict = PROFILE) -> dict:
    user = auth.upsert_github_user(profile)
    return {bifrost_auth.SESSION_COOKIE: bifrost_auth.sign(user["id"])}


def keys_client(engine, cookies=None) -> TestClient:
    app = FastAPI()
    app.include_router(Auth(lambda: engine).router())
    return TestClient(app, base_url="https://testserver", cookies=cookies or {})


# ── encryption ─────────────────────────────────────────────────────────────────


def test_encryption_round_trips_and_is_bound_to_the_user(engine):
    token = keys.encrypt("user-a", "groq", USER_KEY)
    assert USER_KEY not in token and token.startswith("v1:")
    assert keys.decrypt("user-a", "groq", token) == USER_KEY
    # Same ciphertext under another user (or provider) is rejected
    with pytest.raises(HTTPException) as exc:
        keys.decrypt("user-b", "groq", token)
    assert exc.value.status_code == 428


def test_tampered_ciphertext_is_rejected(engine):
    token = keys.encrypt("user-a", "groq", USER_KEY)
    raw = bytearray(base64.urlsafe_b64decode(token[3:]))
    raw[-1] ^= 1
    with pytest.raises(HTTPException):
        keys.decrypt("user-a", "groq", "v1:" + base64.urlsafe_b64encode(bytes(raw)).decode())


@pytest.mark.parametrize("secret", ["", "not-base64!!", base64.urlsafe_b64encode(b"short").decode()])
def test_missing_or_bad_encryption_secret_returns_503(engine, monkeypatch, secret):
    monkeypatch.setenv("KEY_ENCRYPTION_SECRET", secret)
    with pytest.raises(HTTPException) as exc:
        keys.encrypt("user-a", "groq", USER_KEY)
    assert exc.value.status_code == 503


# ── /auth/keys API ─────────────────────────────────────────────────────────────


def test_save_list_and_remove_key(engine):
    auth = Auth(lambda: engine)
    c = keys_client(engine, session_for(auth))

    assert c.get("/auth/keys").json()[0] | {"configured": False} == c.get("/auth/keys").json()[0]
    saved = c.put("/auth/keys/groq", json={"api_key": USER_KEY})
    assert saved.status_code == 200
    assert saved.json()["configured"] is True and saved.json()["last4"] == USER_KEY[-4:]
    assert "api_key" not in saved.json() and USER_KEY not in saved.text

    # Stored encrypted — the plaintext never reaches the database
    with engine.connect() as conn:
        stored = conn.execute(sa.select(bifrost_auth.user_keys_table.c.ciphertext)).scalar_one()
    assert USER_KEY not in stored

    # Replacing keeps exactly one row
    c.put("/auth/keys/groq", json={"api_key": "gsk_second_" + "b" * 40})
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(bifrost_auth.user_keys_table)).scalar() == 1

    assert c.delete("/auth/keys/groq").status_code == 204
    assert c.get("/auth/keys").json()[0]["configured"] is False
    assert c.delete("/auth/keys/groq").status_code == 404


def test_key_api_requires_sign_in_and_validates_input(engine):
    anonymous = keys_client(engine)
    assert anonymous.get("/auth/keys").status_code == 401
    assert anonymous.put("/auth/keys/groq", json={"api_key": USER_KEY}).status_code == 401

    c = keys_client(engine, session_for(Auth(lambda: engine)))
    assert c.put("/auth/keys/groq", json={"api_key": "short"}).status_code == 422
    assert c.put("/auth/keys/groq", json={"api_key": "gsk_has spaces " + "x" * 20}).status_code == 422
    assert c.put("/auth/keys/openai", json={"api_key": USER_KEY}).status_code == 404


def test_key_rejected_by_groq_is_not_saved(engine, monkeypatch):
    def reject(provider, api_key):
        raise HTTPException(status_code=400, detail="Groq rejected that key")

    monkeypatch.setattr(keys, "validate_with_provider", reject)
    c = keys_client(engine, session_for(Auth(lambda: engine)))
    assert c.put("/auth/keys/groq", json={"api_key": USER_KEY}).status_code == 400
    assert c.get("/auth/keys").json()[0]["configured"] is False


def test_users_cannot_see_each_others_keys(engine):
    auth = Auth(lambda: engine)
    keys_client(engine, session_for(auth)).put("/auth/keys/groq", json={"api_key": USER_KEY})
    other = keys_client(engine, session_for(auth, {"id": 8, "login": "other", "name": None, "avatar_url": None}))
    assert other.get("/auth/keys").json()[0]["configured"] is False


# ── apps use the user's key, never the server's ────────────────────────────────


class RecordingGroq:
    """Stands in for groq.Groq and records which key each client was built with."""

    used_keys: list[str] = []

    def __init__(self, api_key):
        RecordingGroq.used_keys.append(api_key)
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        class Message:
            content = (
                '{"summary": "ok", "score": 9, "issues": [], "strengths": ["clear"], "recommendation": "ship it",'
                ' "match_score": 80, "verdict": "good", "gaps": [], "suggestions": [],'
                ' "keywords_matched": [], "keywords_missing": []}'
            )

        class Choice:
            message = Message()

        class Completion:
            choices = [Choice()]

        return Completion()


def test_code_review_requires_sign_in_then_a_saved_key(engine):
    assert TestClient(code_reviewer.app).post("/code-review", json={"code": "x = 1"}).status_code == 401
    signed_in = TestClient(code_reviewer.app, base_url="https://testserver", cookies=session_for(code_reviewer.auth))
    response = signed_in.post("/code-review", json={"code": "x = 1"})
    assert response.status_code == 428
    assert "/settings" in response.json()["detail"]


def test_code_review_runs_on_the_users_own_key(engine, monkeypatch):
    monkeypatch.setattr(code_reviewer, "Groq", RecordingGroq)
    RecordingGroq.used_keys = []
    cookies = session_for(code_reviewer.auth)
    keys_client(engine, cookies).put("/auth/keys/groq", json={"api_key": USER_KEY})

    client = TestClient(code_reviewer.app, base_url="https://testserver", cookies=cookies)
    response = client.post("/code-review", json={"code": "x = 1"})
    assert response.status_code == 200 and response.json()["score"] == 9
    assert RecordingGroq.used_keys == [USER_KEY]


def test_rejected_user_key_points_to_settings(engine, monkeypatch):
    import httpx
    from groq import AuthenticationError

    class RejectingGroq(RecordingGroq):
        def create(self, **kwargs):
            request = httpx.Request("POST", "https://api.groq.com")
            raise AuthenticationError("invalid key", response=httpx.Response(401, request=request), body=None)

    monkeypatch.setattr(code_reviewer, "Groq", RejectingGroq)
    monkeypatch.setitem(
        code_reviewer.app.dependency_overrides, code_reviewer.auth.require_key("groq"), lambda: USER_KEY
    )
    response = TestClient(code_reviewer.app).post("/code-review", json={"code": "x = 1"})
    assert response.status_code == 400 and "Settings" in response.json()["detail"]


def test_resume_analyzer_requires_sign_in_and_uses_the_users_key(engine, monkeypatch):
    pdf = {"resume": ("cv.pdf", b"%PDF-1.4", "application/pdf")}
    assert (
        TestClient(resume.app)
        .post("/resume-analyzer/analyze", files=pdf, data={"job_url": "https://x.example"})
        .status_code
        == 401
    )

    monkeypatch.setattr(resume, "Groq", RecordingGroq)
    RecordingGroq.used_keys = []
    assert resume._analyze_groq("## Resume ...", USER_KEY)["match_score"] == 80
    assert RecordingGroq.used_keys == [USER_KEY]
