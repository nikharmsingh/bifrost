import json
import os
import pathlib
import sys

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from groq import APIError, AuthenticationError, Groq
from pydantic import BaseModel
from sqlalchemy.pool import NullPool

# Vercel installs from requirements.txt, not the uv workspace, so load the shared auth package from the repo
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "packages" / "auth" / "src"))
from bifrost_auth import Auth  # noqa: E402

DATABASE_URL = os.getenv("DATABASE_URL", "")
# NullPool is required for serverless — Neon handles pooling via PgBouncer
engine = sa.create_engine(DATABASE_URL, poolclass=NullPool) if DATABASE_URL else None


def require_db() -> sa.Engine:
    if not engine:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return engine


auth = Auth(require_db)

app = FastAPI(
    title="Code Reviewer",
    description="AI code review that returns a score, issues and suggestions as structured JSON. Uses your own Groq key.",
    version="0.1.0",
    docs_url="/code-review/docs",
    openapi_url="/code-review/openapi.json",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Groq retires models periodically (llama-3.3-70b-versatile was shut down 2026-08-16) — override without a deploy
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


SYSTEM_PROMPT = """\
You are a senior software engineer performing a thorough code review.
Analyze the submitted code and respond with a JSON object using this exact structure:

{
  "summary": "one paragraph overview of the code",
  "score": <integer 1-10, where 10 is production-ready>,
  "issues": [
    {
      "type": "bug | security | performance | style",
      "severity": "high | medium | low",
      "description": "clear explanation of the issue",
      "suggestion": "concrete fix or improvement"
    }
  ],
  "strengths": ["list of things done well"],
  "recommendation": "one sentence overall recommendation"
}

Rules:
- Respond with valid JSON only — no markdown fences, no extra text.
- If the code is empty or nonsensical, return score 1 and explain in summary.
- List all issues you find, from most to least severe.\
"""


class ReviewRequest(BaseModel):
    code: str
    language: str = "python"


class Issue(BaseModel):
    type: str
    severity: str
    description: str
    suggestion: str


class ReviewResponse(BaseModel):
    summary: str
    score: int
    issues: list[Issue]
    strengths: list[str]
    recommendation: str


def _load_ui() -> str:
    try:
        return (pathlib.Path(__file__).parent / "ui.html").read_text()
    except FileNotFoundError:
        return "<!DOCTYPE html><title>Code Reviewer</title><p>UI unavailable — see <a href='/code-review/docs'>API docs</a>.</p>"


UI_HTML = _load_ui()


# Same for every visitor (personal data loads via JS), so serve it from Vercel's regional edge cache.
# Browsers always revalidate; the edge refreshes in the background.
PAGE_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=0, must-revalidate",
    "Vercel-CDN-Cache-Control": "max-age=300, stale-while-revalidate=86400",
}


@app.get("/code-review", response_class=HTMLResponse, summary="Code Reviewer UI")
async def ui(response: Response) -> str:
    response.headers.update(PAGE_CACHE_HEADERS)
    return UI_HTML


@app.get("/code-review/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/code-review", response_model=ReviewResponse)
def review_code(req: ReviewRequest, groq_key: str = Depends(auth.require_key("groq"))) -> dict:
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="code must not be empty")

    if len(req.code) > 10_000:
        raise HTTPException(status_code=400, detail="code exceeds 10,000 character limit")

    try:
        # The signed-in user's own key — the server never spends its own Groq quota
        response = Groq(api_key=groq_key).chat.completions.create(
            model=GROQ_MODEL,
            # Headroom for reasoning tokens, which count toward the limit on reasoning models
            max_tokens=4096,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Review this {req.language} code:\n\n```{req.language}\n{req.code}\n```"},
            ],
            response_format={"type": "json_object"},
        )
    except AuthenticationError:
        raise HTTPException(
            status_code=400, detail="Groq rejected your saved key — update it in Settings (/settings)"
        ) from None
    except APIError as exc:
        raise HTTPException(status_code=502, detail=f"Review model unavailable: {exc.message}") from exc

    try:
        return json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, IndexError, TypeError):
        raise HTTPException(status_code=500, detail="Model returned an unexpected response format") from None
