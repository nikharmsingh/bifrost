from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import load_module

chat = load_module("gateway_chat", "apis/gateway/src/routers/chat.py")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(chat.router, prefix="/chat")
    return TestClient(app)


def _fake_stream(prompt, model, system):
    async def gen():
        yield f"{model}:{prompt}"

    return gen()


def test_requires_api_key_when_configured(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "secret")
    monkeypatch.setattr(chat, "stream_text_async", _fake_stream)
    client = _client()
    assert client.post("/chat/stream", json={"prompt": "hi"}).status_code == 401
    response = client.post("/chat/stream", json={"prompt": "hi"}, headers={"X-API-Key": "secret"})
    assert response.status_code == 200


def test_rejects_models_outside_allowlist(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "")
    monkeypatch.setenv("DEFAULT_MODEL", "default-model")
    monkeypatch.setenv("ALLOWED_MODELS", "cheap-model, other-model")
    monkeypatch.setattr(chat, "stream_text_async", _fake_stream)
    client = _client()

    assert client.post("/chat/stream", json={"prompt": "hi"}).text == "default-model:hi"
    assert client.post("/chat/stream", json={"prompt": "hi", "model": "cheap-model"}).text == "cheap-model:hi"
    assert client.post("/chat/stream", json={"prompt": "hi", "model": "claude-opus-x"}).status_code == 400


def test_settings_parse_comma_separated_origins(monkeypatch):
    from core.config import Settings

    monkeypatch.setenv("ALLOWED_ORIGINS", "https://a.com, https://b.com")
    assert Settings().allowed_origins == ["https://a.com", "https://b.com"]
