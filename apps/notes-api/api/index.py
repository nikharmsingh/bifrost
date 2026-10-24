import hashlib
import hmac
import os
import pathlib
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import APIKeyCookie
from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

DATABASE_URL = os.getenv("DATABASE_URL", "")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
# Signs session and OAuth-state cookies; rotating it signs everyone out
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
# Notes created before accounts existed are assigned to this GitHub user on their first sign-in
NOTES_OWNER_GITHUB_LOGIN = os.getenv("NOTES_OWNER_GITHUB_LOGIN", "").lower()

SESSION_COOKIE = "notes_session"
STATE_COOKIE = "notes_oauth_state"
SESSION_TTL_SECONDS = 30 * 24 * 3600
STATE_TTL_SECONDS = 600
UI_PATH = "/notes/ui"

# NullPool is required for serverless — Neon handles pooling via PgBouncer
engine = create_engine(DATABASE_URL, poolclass=NullPool) if DATABASE_URL else None

metadata = sa.MetaData()
users_table = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("github_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("login", sa.String, nullable=False),
    sa.Column("name", sa.String),
    sa.Column("avatar_url", sa.String),
    sa.Column("created_at", sa.String, nullable=False),
)
notes_table = sa.Table(
    "notes",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("owner_id", sa.String, index=True),
    sa.Column("title", sa.String, nullable=False),
    sa.Column("content", sa.String, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
    sa.Column("updated_at", sa.String, nullable=False),
)


def migrate(db: sa.Engine) -> None:
    metadata.create_all(db)
    # notes predates accounts: create_all won't add columns to an existing table
    columns = {c["name"] for c in sa.inspect(db).get_columns("notes")}
    if "owner_id" not in columns:
        with db.begin() as conn:
            conn.execute(sa.text("ALTER TABLE notes ADD COLUMN owner_id VARCHAR"))
            conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_notes_owner_id ON notes (owner_id)"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if engine:
        migrate(engine)
    yield


app = FastAPI(
    title="Notes API",
    description="Private notes stored in Neon Postgres. Sign in with GitHub to read and write your own notes.",
    version="0.3.0",
    docs_url="/notes/docs",
    openapi_url="/notes/openapi.json",
    lifespan=lifespan,
)

_allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=_allowed_origins, allow_methods=["*"], allow_headers=["*"])

session_scheme = APIKeyCookie(name=SESSION_COOKIE, auto_error=False, description="Browser session from GitHub sign-in")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def require_db() -> sa.Engine:
    if not engine:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return engine


# ── signed cookies ─────────────────────────────────────────────────────────────


def _sign(value: str) -> str:
    if not SESSION_SECRET:
        raise HTTPException(status_code=503, detail="Sign-in is not configured (SESSION_SECRET missing)")
    mac = hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{mac}"


def _unsign(signed: str, max_age: int) -> str | None:
    """Return the payload of a signed `payload:issued_at` value if the signature is valid and unexpired."""
    if not SESSION_SECRET or "." not in signed:
        return None
    value, mac = signed.rsplit(".", 1)
    expected = hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    payload, _, issued_at = value.rpartition(":")
    if not hmac.compare_digest(mac, expected) or not issued_at.isdigit():
        return None
    return payload if time.time() - int(issued_at) <= max_age else None


def _set_cookie(response: Response, name: str, payload: str, max_age: int, path: str) -> None:
    response.set_cookie(
        name,
        _sign(f"{payload}:{int(time.time())}"),
        max_age=max_age,
        path=path,
        httponly=True,
        secure=True,
        samesite="lax",
    )


# ── authentication ─────────────────────────────────────────────────────────────


class User(BaseModel):
    id: str
    login: str
    name: str | None
    avatar_url: str | None


def _user_by_id(conn: sa.Connection, user_id: str) -> dict | None:
    row = conn.execute(sa.select(users_table).where(users_table.c.id == user_id)).mappings().first()
    return dict(row) if row else None


def current_user(session: str | None = Depends(session_scheme)) -> dict:
    user_id = _unsign(session, SESSION_TTL_SECONDS) if session else None
    with require_db().connect() as conn:
        user = _user_by_id(conn, user_id) if user_id else None
    if not user:
        raise HTTPException(status_code=401, detail="Sign in with GitHub to access your notes")
    return user


# ── GitHub OAuth ───────────────────────────────────────────────────────────────


def _require_oauth_config() -> None:
    if not (GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET and SESSION_SECRET):
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured")


def _callback_url(request: Request) -> str:
    # Vercel terminates TLS at the edge, so trust its forwarded scheme
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{request.url.netloc}/notes/auth/callback"


def exchange_code_for_github_user(code: str, redirect_uri: str) -> dict:
    with httpx.Client(timeout=10) as client:
        token_response = client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        try:
            access_token = token_response.json().get("access_token")
        except ValueError:
            access_token = None
        if not access_token:
            raise HTTPException(status_code=400, detail="GitHub sign-in failed — please try again")
        user_response = client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        )
    if user_response.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not read your GitHub profile")
    return user_response.json()


def upsert_github_user(profile: dict) -> dict:
    fields = {"login": profile["login"], "name": profile.get("name"), "avatar_url": profile.get("avatar_url")}
    with require_db().begin() as conn:
        existing = (
            conn.execute(sa.select(users_table).where(users_table.c.github_id == profile["id"])).mappings().first()
        )
        if existing:
            conn.execute(sa.update(users_table).where(users_table.c.id == existing["id"]).values(**fields))
            user = {**existing, **fields}
        else:
            user = {"id": str(uuid4()), "github_id": profile["id"], "created_at": now_iso(), **fields}
            conn.execute(sa.insert(users_table).values(**user))
        if NOTES_OWNER_GITHUB_LOGIN and user["login"].lower() == NOTES_OWNER_GITHUB_LOGIN:
            conn.execute(sa.update(notes_table).where(notes_table.c.owner_id.is_(None)).values(owner_id=user["id"]))
    return user


@app.get("/notes/auth/login", summary="Start GitHub sign-in", include_in_schema=False)
def login(request: Request) -> RedirectResponse:
    _require_oauth_config()
    state = secrets.token_urlsafe(24)
    query = urlencode(
        {"client_id": GITHUB_CLIENT_ID, "redirect_uri": _callback_url(request), "state": state, "scope": ""}
    )
    response = RedirectResponse(f"https://github.com/login/oauth/authorize?{query}", status_code=302)
    _set_cookie(response, STATE_COOKIE, state, STATE_TTL_SECONDS, path="/notes/auth")
    return response


@app.get("/notes/auth/callback", include_in_schema=False)
def oauth_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    _require_oauth_config()
    expected_state = _unsign(request.cookies.get(STATE_COOKIE, ""), STATE_TTL_SECONDS)
    if not code or not expected_state or not hmac.compare_digest(state, expected_state):
        raise HTTPException(status_code=400, detail="Sign-in expired or was tampered with — please try again")
    user = upsert_github_user(exchange_code_for_github_user(code, _callback_url(request)))
    response = RedirectResponse(UI_PATH, status_code=302)
    response.delete_cookie(STATE_COOKIE, path="/notes/auth")
    _set_cookie(response, SESSION_COOKIE, user["id"], SESSION_TTL_SECONDS, path="/notes")
    return response


@app.post("/notes/auth/logout", status_code=204, summary="Sign out")
def logout() -> Response:
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/notes")
    return response


# ── models ─────────────────────────────────────────────────────────────────────


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=50_000)


class Note(BaseModel):
    id: str
    title: str
    content: str
    created_at: str
    updated_at: str


NOTE_COLUMNS = [c for c in notes_table.c if c.name != "owner_id"]


# ── pages, health, account ─────────────────────────────────────────────────────


def _load_ui() -> str:
    try:
        return (pathlib.Path(__file__).parent / "ui.html").read_text()
    except FileNotFoundError:
        return "<!DOCTYPE html><title>Notes</title><p>UI unavailable — see <a href='/notes/docs'>API docs</a>.</p>"


UI_HTML = _load_ui()


# Fixed paths under /notes/ are declared before /notes/{note_id} so they aren't captured as note ids
@app.get(UI_PATH, response_class=HTMLResponse, summary="Notes UI")
def ui() -> str:
    return UI_HTML


@app.get("/notes/health")
def health() -> dict:
    with require_db().connect() as conn:
        conn.execute(sa.text("SELECT 1"))
    return {"status": "ok"}


@app.get("/notes/me", response_model=User, summary="The signed-in user")
def me(user: dict = Depends(current_user)) -> dict:
    return user


# ── notes (always scoped to the caller) ────────────────────────────────────────


def _owned(note_id: str, user: dict):
    return sa.and_(notes_table.c.id == note_id, notes_table.c.owner_id == user["id"])


@app.get("/notes", response_model=list[Note], summary="List your notes")
def list_notes(user: dict = Depends(current_user)) -> list[dict]:
    with require_db().connect() as conn:
        rows = conn.execute(
            sa.select(*NOTE_COLUMNS)
            .where(notes_table.c.owner_id == user["id"])
            .order_by(notes_table.c.updated_at.desc())
        ).mappings()
        return [dict(r) for r in rows]


@app.post("/notes", response_model=Note, status_code=201, summary="Create a note")
def create_note(body: NoteIn, user: dict = Depends(current_user)) -> dict:
    now = now_iso()
    note = {"id": str(uuid4()), "title": body.title, "content": body.content, "created_at": now, "updated_at": now}
    with require_db().begin() as conn:
        conn.execute(sa.insert(notes_table).values(owner_id=user["id"], **note))
    return note


# Another user's note is reported as 404, not 403, so ids can't be probed
@app.get("/notes/{note_id}", response_model=Note, summary="Get a note")
def get_note(note_id: str, user: dict = Depends(current_user)) -> dict:
    with require_db().connect() as conn:
        row = conn.execute(sa.select(*NOTE_COLUMNS).where(_owned(note_id, user))).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return dict(row)


@app.put("/notes/{note_id}", response_model=Note, summary="Update a note")
def update_note(note_id: str, body: NoteIn, user: dict = Depends(current_user)) -> dict:
    with require_db().begin() as conn:
        result = (
            conn.execute(
                sa.update(notes_table)
                .where(_owned(note_id, user))
                .values(title=body.title, content=body.content, updated_at=now_iso())
                .returning(*NOTE_COLUMNS)
            )
            .mappings()
            .first()
        )
    if not result:
        raise HTTPException(status_code=404, detail="Note not found")
    return dict(result)


@app.delete("/notes/{note_id}", status_code=204, summary="Delete a note")
def delete_note(note_id: str, user: dict = Depends(current_user)) -> None:
    with require_db().begin() as conn:
        result = conn.execute(sa.delete(notes_table).where(_owned(note_id, user)))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Note not found")
