import json
import os
import pathlib
import re
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, field_validator
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
    # JSON list of lowercase tags
    sa.Column("tags", sa.String, nullable=False, server_default="[]"),
    sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.String, nullable=False),
    sa.Column("updated_at", sa.String, nullable=False),
)


# Columns added after the table first shipped: create_all won't add columns to an existing table
ADDED_COLUMNS = {
    "owner_id": [
        "ALTER TABLE notes ADD COLUMN owner_id VARCHAR",
        "CREATE INDEX IF NOT EXISTS ix_notes_owner_id ON notes (owner_id)",
    ],
    "tags": ["ALTER TABLE notes ADD COLUMN tags VARCHAR NOT NULL DEFAULT '[]'"],
    "pinned": ["ALTER TABLE notes ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT FALSE"],
}


def migrate(db: sa.Engine) -> None:
    auth_metadata.create_all(db)
    metadata.create_all(db)
    columns = {c["name"] for c in sa.inspect(db).get_columns("notes")}
    missing = [
        statement for name, statements in ADDED_COLUMNS.items() if name not in columns for statement in statements
    ]
    if missing:
        with db.begin() as conn:
            for statement in missing:
                conn.execute(sa.text(statement))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if engine:
        migrate(engine)
    yield


app = FastAPI(
    title="Notes API",
    description="Private notes stored in Neon Postgres. Sign in with GitHub (shared Bifrost sign-in) to use them.",
    version="0.5.0",
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


MAX_TAGS = 10
_TAG = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")


def normalize_tags(tags: list[str] | None) -> list[str] | None:
    """Lowercase, trim, drop a leading '#', dedupe (keeping order); reject anything else."""
    if tags is None:
        return None
    cleaned = list(dict.fromkeys(t.strip().lstrip("#").lower() for t in tags if t.strip().lstrip("#")))
    if len(cleaned) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags")
    if bad := [t for t in cleaned if not _TAG.match(t)]:
        raise ValueError(f"tags may use letters, digits, - and _ (max 32 chars): {', '.join(bad)}")
    return cleaned


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=50_000)
    # Left out on update = unchanged
    tags: list[str] | None = None
    pinned: bool | None = None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, tags: list[str] | None) -> list[str] | None:
        return normalize_tags(tags)


class NotePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, max_length=50_000)
    tags: list[str] | None = None
    pinned: bool | None = None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, tags: list[str] | None) -> list[str] | None:
        return normalize_tags(tags)


class Note(BaseModel):
    id: str
    title: str
    content: str
    tags: list[str]
    pinned: bool
    created_at: str
    updated_at: str


NOTE_COLUMNS = [c for c in notes_table.c if c.name != "owner_id"]


def to_note(row) -> dict:
    return {**row, "tags": json.loads(row["tags"] or "[]"), "pinned": bool(row["pinned"])}


def stored_values(fields: dict) -> dict:
    """API fields → column values (tags are stored as JSON); None means 'leave unchanged'."""
    values = {k: v for k, v in fields.items() if v is not None}
    if "tags" in values:
        values["tags"] = json.dumps(values["tags"])
    return values


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
            .order_by(notes_table.c.pinned.desc(), notes_table.c.updated_at.desc())
        ).mappings()
        return [to_note(r) for r in rows]


@app.post("/notes", response_model=Note, status_code=201, summary="Create a note")
def create_note(body: NoteIn, user: dict = Depends(current_user)) -> dict:
    now = now_iso()
    note = {
        "id": str(uuid4()),
        "title": body.title,
        "content": body.content,
        "tags": body.tags or [],
        "pinned": bool(body.pinned),
        "created_at": now,
        "updated_at": now,
    }
    with require_db().begin() as conn:
        conn.execute(sa.insert(notes_table).values(owner_id=user["id"], **stored_values(note)))
    return note


# Another user's note is reported as 404, not 403, so ids can't be probed
@app.get("/notes/{note_id}", response_model=Note, summary="Get a note")
def get_note(note_id: str, user: dict = Depends(current_user)) -> dict:
    with require_db().connect() as conn:
        row = conn.execute(sa.select(*NOTE_COLUMNS).where(_owned(note_id, user))).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return to_note(row)


def _update(note_id: str, user: dict, values: dict) -> dict:
    with require_db().begin() as conn:
        result = (
            conn.execute(sa.update(notes_table).where(_owned(note_id, user)).values(**values).returning(*NOTE_COLUMNS))
            .mappings()
            .first()
        )
    if not result:
        raise HTTPException(status_code=404, detail="Note not found")
    return to_note(result)


@app.put("/notes/{note_id}", response_model=Note, summary="Update a note")
def update_note(note_id: str, body: NoteIn, user: dict = Depends(current_user)) -> dict:
    return _update(note_id, user, {**stored_values(body.model_dump()), "updated_at": now_iso()})


@app.patch("/notes/{note_id}", response_model=Note, summary="Change some fields of a note")
def patch_note(note_id: str, body: NotePatch, user: dict = Depends(current_user)) -> dict:
    values = stored_values(body.model_dump())
    if not values:
        return get_note(note_id, user)
    # Pinning is organisation, not an edit, so it doesn't move the note's "updated" time
    if values.keys() - {"pinned"}:
        values["updated_at"] = now_iso()
    return _update(note_id, user, values)


@app.delete("/notes/{note_id}", status_code=204, summary="Delete a note")
def delete_note(note_id: str, user: dict = Depends(current_user)) -> None:
    with require_db().begin() as conn:
        result = conn.execute(sa.delete(notes_table).where(_owned(note_id, user)))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Note not found")
