import os
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

DATABASE_URL = os.getenv("DATABASE_URL", "")
# When set, write endpoints require a matching X-API-Key header
NOTES_API_KEY = os.getenv("NOTES_API_KEY", "")

# NullPool is required for serverless — Neon handles pooling via PgBouncer
engine = create_engine(DATABASE_URL, poolclass=NullPool) if DATABASE_URL else None

metadata = sa.MetaData()
notes_table = sa.Table(
    "notes",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("title", sa.String, nullable=False),
    sa.Column("content", sa.String, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
    sa.Column("updated_at", sa.String, nullable=False),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if engine:
        metadata.create_all(engine)
    yield


app = FastAPI(
    title="Notes API",
    description="Create, read, update and delete notes stored in Neon Postgres. Writes require X-API-Key.",
    version="0.2.0",
    docs_url="/notes/docs",
    openapi_url="/notes/openapi.json",
    lifespan=lifespan,
)

_allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=_allowed_origins, allow_methods=["*"], allow_headers=["*"])


def require_db():
    if not engine:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return engine


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if NOTES_API_KEY and not secrets.compare_digest(x_api_key, NOTES_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=50_000)


class Note(BaseModel):
    id: str
    title: str
    content: str
    created_at: str
    updated_at: str


# Declared before /notes/{note_id} so "health" isn't captured as a note id
@app.get("/notes/health")
def health() -> dict:
    with require_db().connect() as conn:
        conn.execute(sa.text("SELECT 1"))
    return {"status": "ok"}


@app.get("/notes", response_model=list[Note])
def list_notes() -> list[dict]:
    with require_db().connect() as conn:
        rows = conn.execute(sa.select(notes_table).order_by(notes_table.c.created_at.desc())).mappings().all()
    return [dict(r) for r in rows]


@app.post("/notes", response_model=Note, status_code=201, dependencies=[Depends(require_api_key)])
def create_note(body: NoteIn) -> dict:
    now = datetime.now(UTC).isoformat()
    note = {"id": str(uuid4()), "title": body.title, "content": body.content, "created_at": now, "updated_at": now}
    with require_db().connect() as conn:
        conn.execute(sa.insert(notes_table).values(**note))
        conn.commit()
    return note


@app.get("/notes/{note_id}", response_model=Note)
def get_note(note_id: str) -> dict:
    with require_db().connect() as conn:
        row = conn.execute(sa.select(notes_table).where(notes_table.c.id == note_id)).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    return dict(row)


@app.put("/notes/{note_id}", response_model=Note, dependencies=[Depends(require_api_key)])
def update_note(note_id: str, body: NoteIn) -> dict:
    now = datetime.now(UTC).isoformat()
    with require_db().connect() as conn:
        result = (
            conn.execute(
                sa.update(notes_table)
                .where(notes_table.c.id == note_id)
                .values(title=body.title, content=body.content, updated_at=now)
                .returning(*notes_table.c)
            )
            .mappings()
            .first()
        )
        conn.commit()
    if not result:
        raise HTTPException(status_code=404, detail="Note not found")
    return dict(result)


@app.delete("/notes/{note_id}", status_code=204, dependencies=[Depends(require_api_key)])
def delete_note(note_id: str) -> None:
    with require_db().connect() as conn:
        result = conn.execute(sa.delete(notes_table).where(notes_table.c.id == note_id))
        conn.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Note not found")
