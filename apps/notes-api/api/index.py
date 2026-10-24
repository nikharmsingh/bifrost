import os
import pathlib
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

# Vercel installs from requirements.txt, not the uv workspace, so load the shared auth package from the repo
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "packages" / "auth" / "src"))
from bifrost_auth import Auth, User  # noqa: E402
from bifrost_auth import metadata as auth_metadata  # noqa: E402

DATABASE_URL = os.getenv("DATABASE_URL", "")
# Notes created before accounts existed are assigned to this GitHub user when they sign in
NOTES_OWNER_GITHUB_LOGIN = os.getenv("NOTES_OWNER_GITHUB_LOGIN", "").lower()
UI_PATH = "/notes/ui"

# NullPool is required for serverless — Neon handles pooling via PgBouncer
engine = create_engine(DATABASE_URL, poolclass=NullPool) if DATABASE_URL else None

metadata = sa.MetaData()
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
    auth_metadata.create_all(db)
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
    description="Private notes stored in Neon Postgres. Sign in with GitHub (shared Bifrost sign-in) to use them.",
    version="0.4.0",
    docs_url="/notes/docs",
    openapi_url="/notes/openapi.json",
    lifespan=lifespan,
)

_allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=_allowed_origins, allow_methods=["*"], allow_headers=["*"])


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def require_db() -> sa.Engine:
    if not engine:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return engine


# ── authentication (shared Bifrost sign-in at /auth/*) ─────────────────────────

auth = Auth(require_db)


_legacy_notes_claimed = False


def current_user(user: dict = Depends(auth.current_user)) -> dict:
    # Notes from before accounts existed go to the configured owner (once per instance, not every request)
    global _legacy_notes_claimed
    if not _legacy_notes_claimed and NOTES_OWNER_GITHUB_LOGIN and user["login"].lower() == NOTES_OWNER_GITHUB_LOGIN:
        with require_db().begin() as conn:
            conn.execute(sa.update(notes_table).where(notes_table.c.owner_id.is_(None)).values(owner_id=user["id"]))
        _legacy_notes_claimed = True
    return user


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

# Same for every visitor (personal data loads via JS), so serve it from Vercel's regional edge cache.
# Browsers always revalidate; the edge refreshes in the background.
PAGE_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=0, must-revalidate",
    "Vercel-CDN-Cache-Control": "max-age=300, stale-while-revalidate=86400",
}


@app.get(UI_PATH, response_class=HTMLResponse, summary="Notes UI")
def ui(response: Response) -> str:
    response.headers.update(PAGE_CACHE_HEADERS)
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
