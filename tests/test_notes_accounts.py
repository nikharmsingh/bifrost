"""Notes on top of shared sign-in: per-user isolation, legacy-note claiming and the schema migration."""

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

import bifrost_auth
from conftest import load_module

notes = load_module("notes_api_accounts", "apps/notes-api/api/index.py")

OWNER = {"id": 101, "login": "Owner", "name": "Owner Person", "avatar_url": "https://avatars.example/1"}
OTHER = {"id": 202, "login": "someone-else", "name": None, "avatar_url": None}


@pytest.fixture
def db(monkeypatch):
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    notes.migrate(engine)
    monkeypatch.setattr(notes, "engine", engine)
    monkeypatch.setattr(notes, "NOTES_OWNER_GITHUB_LOGIN", "owner")
    monkeypatch.setattr(notes, "_legacy_notes_claimed", False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    return engine


def signed_in(profile: dict) -> TestClient:
    """A client holding a real signed Bifrost session (the OAuth flow itself is covered in test_auth)."""
    user = notes.auth.upsert_github_user(profile)
    return TestClient(
        notes.app, base_url="https://testserver", cookies={bifrost_auth.SESSION_COOKIE: bifrost_auth.sign(user["id"])}
    )


def create_note(c: TestClient, title: str = "Hello") -> dict:
    response = c.post("/notes", json={"title": title, "content": "body"})
    assert response.status_code == 201
    return response.json()


def test_unauthenticated_requests_are_rejected(db):
    c = TestClient(notes.app, base_url="https://testserver")
    assert c.get("/notes").status_code == 401
    assert c.post("/notes", json={"title": "x", "content": "y"}).status_code == 401
    assert c.get("/notes/me").status_code == 401


def test_shared_session_identifies_the_user(db):
    assert signed_in(OWNER).get("/notes/me").json()["login"] == "Owner"


def test_notes_are_private_to_their_owner(db):
    alice = signed_in(OWNER)
    note = create_note(alice)
    bob = signed_in(OTHER)

    assert bob.get("/notes").json() == []
    # Another user's note looks like it doesn't exist
    assert bob.get(f"/notes/{note['id']}").status_code == 404
    assert bob.put(f"/notes/{note['id']}", json={"title": "hacked", "content": ""}).status_code == 404
    assert bob.delete(f"/notes/{note['id']}").status_code == 404

    assert [n["title"] for n in alice.get("/notes").json()] == ["Hello"]
    assert alice.put(f"/notes/{note['id']}", json={"title": "Edited", "content": "x"}).json()["title"] == "Edited"
    assert alice.delete(f"/notes/{note['id']}").status_code == 204


def test_pre_account_notes_are_claimed_only_by_the_configured_owner(db):
    with db.begin() as conn:
        conn.execute(
            sa.insert(notes.notes_table).values(
                id="legacy", owner_id=None, title="Old", content="", created_at="2026-01-01", updated_at="2026-01-01"
            )
        )
    assert signed_in(OTHER).get("/notes").json() == []
    assert [n["id"] for n in signed_in(OWNER).get("/notes").json()] == ["legacy"]


def test_bearer_tokens_are_not_accepted(db):
    c = TestClient(notes.app, base_url="https://testserver")
    assert c.get("/notes", headers={"Authorization": "Bearer bnt_anything"}).status_code == 401


def test_migrate_adds_owner_column_and_users_table_to_legacy_schema():
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE notes (id VARCHAR PRIMARY KEY, title VARCHAR, content VARCHAR, "
                "created_at VARCHAR, updated_at VARCHAR)"
            )
        )
        conn.execute(sa.text("INSERT INTO notes VALUES ('n1', 't', 'c', 'a', 'b')"))
    notes.migrate(engine)
    notes.migrate(engine)  # idempotent
    inspector = sa.inspect(engine)
    assert "owner_id" in {c["name"] for c in inspector.get_columns("notes")}
    assert "users" in inspector.get_table_names()
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT owner_id FROM notes WHERE id = 'n1'")).scalar() is None
