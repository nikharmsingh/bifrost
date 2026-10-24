import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.config import get_settings
from llm.streaming import stream_text_async


def require_api_key(x_api_key: str = Header(default="")) -> None:
    expected = get_settings().gateway_api_key
    if expected and not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


router = APIRouter(dependencies=[Depends(require_api_key)])


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=50_000)
    model: str | None = None
    system: str = Field(default="You are a helpful assistant.", max_length=10_000)


def resolve_model(requested: str | None) -> str:
    settings = get_settings()
    if requested is None or requested == settings.default_model:
        return settings.default_model
    if requested not in settings.allowed_models:
        raise HTTPException(status_code=400, detail=f"Model '{requested}' is not allowed")
    return requested


@router.post("/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    model = resolve_model(req.model)

    async def _generate():
        async for chunk in stream_text_async(req.prompt, model=model, system=req.system):
            yield chunk

    return StreamingResponse(_generate(), media_type="text/plain")
