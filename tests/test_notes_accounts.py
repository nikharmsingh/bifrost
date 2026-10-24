from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from conftest import load_module

notes = load_module("notes_api_accounts", "apps/notes-api/api/index.py")

OWNER = {"id": 101, "login": "Owner", "name": "Owner Person", "avatar_url": "https://avatars.example/1"}
OTHER = {"id": 202, "login": "someone-else", "name": None, "avatar_url": None}


@pytest.fixture
def db(monkeypatch):
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    notes.migrate(engine)
    monkeypatch.setattr(notes, "engine", engine)
    monkeypatch.setattr(notes, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(notes, "GITHUB_CLIENT_ID", "client-id")
    monkeypatch.setattr(notes, "GITHUB_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(notes, "NOTES_OWNER_GITHUB_LOGIN", "owner")
    return engine


def client() -> TestClient:
    # Session cookies are Secure, so the client must speak https to send them back
    return TestClient(notes.app, base_url="https://testserver", follow_redirects=False)


def sign_in(monkeypatch, profile: dict) -> TestClient:
    monkeypatch.setattr(notes, "exchange_code_for_github_user", lambda code, redirect_uri: profile)
    c = client()
    login = c.get("/notes/auth/login")
    assert login.status_code == 302
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    callback = c.get("/notes/auth/callback", params={"code": "abc", "state": state})
    assert callback.status_code == 302 and callback.headers["location"] == "/notes/ui"
    return c


def create_note(c: TestClient, title: str = "Hello", **kwargs) -> dict:
    response = c.post("/notes", json={"title": title, "content": "body"}, **kwargs)
    assert response.status_code == 201
    return response.json()


def test_unauthenticated_requests_are_rejected(db):
    c = client()
    assert c.get("/notes").status_code == 401
    assert c.post("/notes", json={"title": "x", "content": "y"}).status_code == 401
    assert c.get("/notes/me").status_code == 401


def test_login_redirects_to_github_with_state(db):
    response = client().get("/notes/auth/login")
    location = urlparse(response.headers["location"])
    assert location.netloc == "github.com"
    query = parse_qs(location.query)
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["https://testserver/notes/auth/callback"]
    assert notes.STATE_COOKIE in response.cookies


def test_callback_rejects_missing_or_forged_state(db, monkeypatch):
    monkeypatch.setattr(notes, "exchange_code_for_github_user", lambda code, redirect_uri: OWNER)
    c = client()
    assert c.get("/notes/auth/callback", params={"code": "abc", "state": "x"}).status_code == 400
    c.get("/notes/auth/login")
    assert c.get("/notes/auth/callback", params={"code": "abc", "state": "forged"}).status_code == 400


def test_sign_in_creates_session(db, monkeypatch):
    c = sign_in(monkeypatch, OWNER)
    me = c.get("/notes/me").json()
    assert me["login"] == "Owner" and me["name"] == "Owner Person"


def test_tampered_session_cookie_is_rejected(db, monkeypatch):
    session = sign_in(monkeypatch, OWNER).cookies.get(notes.SESSION_COOKIE)
    tampered = session[:-1] + ("0" if session[-1] != "0" else "1")
    # A fresh client sends only the tampered cookie
    forged = TestClient(notes.app, base_url="https://testserver", cookies={notes.SESSION_COOKIE: tampered})
    assert forged.get("/notes/me").status_code == 401
    assert (
        TestClient(notes.app, base_url="https://testserver", cookies={notes.SESSION_COOKIE: session})
        .get("/notes/me")
        .status_code
        == 200
    )


def test_notes_are_private_to_their_owner(db, monkeypatch):
    alice = sign_in(monkeypatch, OWNER)
    note = create_note(alice)
    bob = sign_in(monkeypatch, OTHER)

    assert bob.get("/notes").json() == []
    # Another user's note looks like it doesn't exist
    assert bob.get(f"/notes/{note['id']}").status_code == 404
    assert bob.put(f"/notes/{note['id']}", json={"title": "hacked", "content": ""}).status_code == 404
    assert bob.delete(f"/notes/{note['id']}").status_code == 404

    assert [n["title"] for n in alice.get("/notes").json()] == ["Hello"]
    assert alice.put(f"/notes/{note['id']}", json={"title": "Edited", "content": "x"}).json()["title"] == "Edited"
    assert alice.delete(f"/notes/{note['id']}").status_code == 204


def test_pre_account_notes_are_claimed_only_by_the_configured_owner(db, monkeypatch):
    with db.begin() as conn:
        conn.execute(
            sa.insert(notes.notes_table).values(
                id="legacy", owner_id=None, title="Old", content="", created_at="2026-01-01", updated_at="2026-01-01"
            )
        )
    stranger = sign_in(monkeypatch, OTHER)
    assert stranger.get("/notes").json() == []

    owner = sign_in(monkeypatch, OWNER)
    assert [n["id"] for n in owner.get("/notes").json()] == ["legacy"]


def test_api_tokens_authenticate_and_can_be_revoked(db, monkeypatch):
    c = sign_in(monkeypatch, OWNER)
    created = c.post("/notes/tokens", json={"name": "script"}).json()
    token = created["token"]
    assert token.startswith("bnt_") and created["display_prefix"] == token[:10]

    # Only the hash is stored
    with db.connect() as conn:
        stored = conn.execute(sa.select(notes.tokens_table.c.token_hash)).scalar_one()
    assert stored == notes.hash_token(token) and token not in stored

    api = client()
    headers = {"Authorization": f"Bearer {token}"}
    create_note(api, "From script", headers=headers)
    assert [n["title"] for n in api.get("/notes", headers=headers).json()] == ["From script"]
    assert c.get("/notes/tokens").json()[0]["last_used_at"] is not None

    # A token can't mint more tokens
    assert api.post("/notes/tokens", json={"name": "x"}, headers=headers).status_code == 401

    assert c.delete(f"/notes/tokens/{created['id']}").status_code == 204
    assert api.get("/notes", headers=headers).status_code == 401


def test_users_cannot_revoke_each_others_tokens(db, monkeypatch):
    owner = sign_in(monkeypatch, OWNER)
    token_id = owner.post("/notes/tokens", json={"name": "mine"}).json()["id"]
    other = sign_in(monkeypatch, OTHER)
    assert other.delete(f"/notes/tokens/{token_id}").status_code == 404
    assert len(owner.get("/notes/tokens").json()) == 1


def test_logout_clears_session(db, monkeypatch):
    c = sign_in(monkeypatch, OWNER)
    assert c.post("/notes/auth/logout").status_code == 204
    assert c.get("/notes/me").status_code == 401


def test_sign_in_unconfigured_returns_503(db, monkeypatch):
    monkeypatch.setattr(notes, "GITHUB_CLIENT_SECRET", "")
    assert client().get("/notes/auth/login").status_code == 503


def test_migrate_adds_owner_column_to_legacy_notes_table():
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE notes (id VARCHAR PRIMARY KEY, title VARCHAR, content VARCHAR, created_at VARCHAR, updated_at VARCHAR)"
            )
        )
        conn.execute(sa.text("INSERT INTO notes VALUES ('n1', 't', 'c', 'a', 'b')"))
    notes.migrate(engine)
    notes.migrate(engine)  # idempotent
    columns = {c["name"] for c in sa.inspect(engine).get_columns("notes")}
    assert "owner_id" in columns
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT owner_id FROM notes WHERE id = 'n1'")).scalar() is None
