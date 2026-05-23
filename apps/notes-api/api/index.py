from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Notes API", version="0.1.0", docs_url="/notes/docs", openapi_url="/notes/openapi.json")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store — resets on cold start (fine for demo)
_notes: dict[str, dict] = {}


class NoteIn(BaseModel):
    title: str
    content: str


class Note(BaseModel):
    id: str
    title: str
    content: str
    created_at: str
    updated_at: str


@app.get("/notes", response_model=list[Note])
async def list_notes() -> list[dict]:
    return list(_notes.values())


@app.post("/notes", response_model=Note, status_code=201)
async def create_note(body: NoteIn) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    note = {"id": str(uuid4()), "title": body.title, "content": body.content, "created_at": now, "updated_at": now}
    _notes[note["id"]] = note
    return note


@app.get("/notes/{note_id}", response_model=Note)
async def get_note(note_id: str) -> dict:
    note = _notes.get(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.put("/notes/{note_id}", response_model=Note)
async def update_note(note_id: str, body: NoteIn) -> dict:
    note = _notes.get(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    note["title"] = body.title
    note["content"] = body.content
    note["updated_at"] = datetime.now(timezone.utc).isoformat()
    return note


@app.delete("/notes/{note_id}", status_code=204)
async def delete_note(note_id: str) -> None:
    if note_id not in _notes:
        raise HTTPException(status_code=404, detail="Note not found")
    del _notes[note_id]
