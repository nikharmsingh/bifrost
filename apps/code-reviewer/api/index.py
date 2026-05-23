import json
import os

from anthropic import Anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="Code Reviewer",
    version="0.1.0",
    docs_url="/code-review/docs",
    openapi_url="/code-review/openapi.json",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_client: Anthropic | None = None


def get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not set")
        _client = Anthropic(api_key=api_key)
    return _client


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


@app.get("/code-review/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/code-review", response_model=ReviewResponse)
async def review_code(req: ReviewRequest) -> dict:
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="code must not be empty")

    if len(req.code) > 10_000:
        raise HTTPException(status_code=400, detail="code exceeds 10,000 character limit")

    message = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Review this {req.language} code:\n\n```{req.language}\n{req.code}\n```",
            }
        ],
    )

    try:
        return json.loads(message.content[0].text)
    except (json.JSONDecodeError, IndexError):
        raise HTTPException(status_code=500, detail="Claude returned an unexpected response format")
