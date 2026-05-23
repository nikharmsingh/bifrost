from fastapi import FastAPI, Request
from core.logging import get_logger

logger = get_logger(__name__)
app = FastAPI(title="Webhooks", version="0.1.0")


@app.post("/webhook/{event}")
async def handle_webhook(event: str, request: Request) -> dict:
    payload = await request.json()
    logger.info("Received webhook event=%s payload=%s", event, payload)
    # TODO: route events to handlers
    return {"received": True, "event": event}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
