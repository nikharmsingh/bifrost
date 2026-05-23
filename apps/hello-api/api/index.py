import random
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="Hello API",
    description="A simple FastAPI app deployed on Vercel",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

QUOTES: list[dict] = [
    {"id": 1, "text": "Code is like humor. When you have to explain it, it's bad.", "author": "Cory House"},
    {"id": 2, "text": "First, solve the problem. Then, write the code.", "author": "John Johnson"},
    {"id": 3, "text": "Any fool can write code that a computer can understand. Good programmers write code that humans can understand.", "author": "Martin Fowler"},
    {"id": 4, "text": "Make it work, make it right, make it fast.", "author": "Kent Beck"},
    {"id": 5, "text": "The best code is no code at all.", "author": "Jeff Atwood"},
]


class Quote(BaseModel):
    id: int
    text: str
    author: str


class APIInfo(BaseModel):
    name: str
    version: str
    docs: str
    endpoints: list[str]


@app.get("/", response_model=APIInfo)
async def root() -> APIInfo:
    return APIInfo(
        name="Hello API",
        version="0.1.0",
        docs="/docs",
        endpoints=["/health", "/quotes", "/quotes/random", "/quotes/{id}", "/utils/uuid", "/utils/timestamp"],
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/quotes", response_model=list[Quote])
async def list_quotes() -> list[dict]:
    return QUOTES


@app.get("/quotes/random", response_model=Quote)
async def random_quote() -> dict:
    return random.choice(QUOTES)


@app.get("/quotes/{quote_id}", response_model=Quote)
async def get_quote(quote_id: int) -> dict:
    match = next((q for q in QUOTES if q["id"] == quote_id), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Quote {quote_id} not found")
    return match


@app.get("/utils/uuid")
async def generate_uuid() -> dict:
    return {"uuid": str(uuid.uuid4())}


@app.get("/utils/timestamp")
async def timestamp() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "iso": now.isoformat(),
        "unix": int(now.timestamp()),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
    }
