import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from groq import Groq
from pydantic import BaseModel

app = FastAPI(
    title="Code Reviewer",
    description="AI code review that returns a score, issues and suggestions as structured JSON (Groq Llama 3.3).",
    version="0.1.0",
    docs_url="/code-review/docs",
    openapi_url="/code-review/openapi.json",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_client: Groq | None = None


def get_client() -> Groq:
    global _client
    if _client is None:
        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=500, detail="GROQ_API_KEY is not set")
        _client = Groq(api_key=api_key)
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
def review_code(req: ReviewRequest) -> dict:
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="code must not be empty")

    if len(req.code) > 10_000:
        raise HTTPException(status_code=400, detail="code exceeds 10,000 character limit")

    response = get_client().chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=2048,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Review this {req.language} code:\n\n```{req.language}\n{req.code}\n```"},
        ],
        response_format={"type": "json_object"},
    )

    try:
        return json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, IndexError):
        raise HTTPException(status_code=500, detail="Model returned an unexpected response format") from None
