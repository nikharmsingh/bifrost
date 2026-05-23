from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from core.logging import get_logger
from .routers import chat

logger = get_logger(__name__)

app = FastAPI(title="Gateway API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router, prefix="/chat", tags=["chat"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
