"""Shared Bifrost sign-in (packages/auth) — the OAuth flow, session cookie and redirect safety."""

from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

import bifrost_auth
from bifrost_auth import Auth

PROFILE = {"id": 101, "login": "octo", "name": "Octo Cat", "avatar_url": "https://avatars.example/1"}


@pytest.fixture
def auth(monkeypatch):
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(bifrost_auth, "exchange_code_for_github_user", lambda code, redirect_uri: PROFILE)
    a = Auth(lambda: engine)
    a.create_tables()
    return a


def client(auth: Auth) -> TestClient:
    app = FastAPI()
    app.include_router(auth.router())

    # Stand-in for another Bifrost app reading the same site-wide session
    @app.get("/other-app/whoami")
    def whoami(user: dict | None = Depends(auth.optional_user)) -> dict:
        return {"login": user["login"] if user else None}

    # Session cookies are Secure, so the client must speak https to send them back
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def sign_in(c: TestClient, next_path: str | None = None):
    login = c.get("/auth/login", params={"next": next_path} if next_path else {})
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    return c.get("/auth/callback", params={"code": "abc", "state": state})


def test_login_redirects_to_github_with_shared_callback(auth):
    response = client(auth).get("/auth/login")
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert location.netloc == "github.com"
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["https://testserver/auth/callback"]
    assert bifrost_auth.STATE_COOKIE in response.cookies


def test_session_cookie_is_site_wide_and_seen_by_other_apps(auth):
    c = client(auth)
    callback = sign_in(c)
    cookie = callback.headers["set-cookie"]
    assert "bifrost_session=" in cookie and "Path=/;" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert c.get("/auth/me").json()["login"] == "octo"
    assert c.get("/other-app/whoami").json() == {"login": "octo"}


@pytest.mark.parametrize(
    ("requested", "landing"),
    [
        ("/notes/ui", "/notes/ui"),
        (None, "/"),
        ("https://evil.example", "/"),
        ("//evil.example/path", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
    ],
)
def test_next_redirect_stays_on_site(auth, requested, landing):
    assert sign_in(client(auth), requested).headers["location"] == landing


def test_callback_rejects_missing_or_forged_state(auth):
    c = client(auth)
    assert c.get("/auth/callback", params={"code": "abc", "state": "x"}).status_code == 400
    c.get("/auth/login")
    assert c.get("/auth/callback", params={"code": "abc", "state": "forged"}).status_code == 400


def test_tampered_session_cookie_is_rejected(auth):
    session = bifrost_auth.sign(auth.upsert_github_user(PROFILE)["id"])
    tampered = session[:-1] + ("0" if session[-1] != "0" else "1")
    app = client(auth).app
    forged = TestClient(app, base_url="https://testserver", cookies={bifrost_auth.SESSION_COOKIE: tampered})
    assert forged.get("/auth/me").status_code == 401
    valid = TestClient(app, base_url="https://testserver", cookies={bifrost_auth.SESSION_COOKIE: session})
    assert valid.get("/auth/me").status_code == 200


def test_sign_in_again_updates_profile_without_duplicating_user(auth, monkeypatch):
    c = client(auth)
    sign_in(c)
    renamed = {**PROFILE, "name": "Renamed"}
    monkeypatch.setattr(bifrost_auth, "exchange_code_for_github_user", lambda code, redirect_uri: renamed)
    sign_in(c)
    assert c.get("/auth/me").json()["name"] == "Renamed"
    with auth._get_engine().connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(bifrost_auth.users_table)).scalar() == 1


def test_logout_clears_session(auth):
    c = client(auth)
    sign_in(c)
    assert c.post("/auth/logout").status_code == 204
    assert c.get("/auth/me").status_code == 401
    assert c.get("/other-app/whoami").json() == {"login": None}


def test_unconfigured_sign_in_returns_503(auth, monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "")
    assert client(auth).get("/auth/login").status_code == 503
