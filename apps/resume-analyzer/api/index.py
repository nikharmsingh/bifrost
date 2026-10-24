import asyncio
import io
import ipaddress
import json
import os
import pathlib
import sys
from urllib.parse import urljoin, urlparse

import httpx
import pypdf
import sqlalchemy as sa
from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
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
    title="Resume Analyzer",
    description="Match a PDF resume against a job posting URL — match score, gaps and keywords. Uses your own Groq key.",
    version="0.1.0",
    docs_url="/resume-analyzer/docs",
    openapi_url="/resume-analyzer/openapi.json",
)

_allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=_allowed_origins, allow_methods=["*"], allow_headers=["*"])

MAX_RESUME_CHARS = 5_000
MAX_JOB_CHARS = 5_000
MAX_PDF_BYTES = 4 * 1024 * 1024  # 4 MB
MAX_REDIRECTS = 5

GROQ_SYSTEM = """\
You are an expert recruiter and career coach. Analyse how well the provided resume matches the job posting.
Respond with a JSON object using exactly this structure — no extra keys, no markdown fences:

{
  "match_score": <integer 0-100>,
  "verdict": "<one-sentence summary of the fit>",
  "strengths": ["<strength>", ...],
  "gaps": ["<missing skill or experience>", ...],
  "suggestions": ["<actionable tip>", ...],
  "keywords_matched": ["<keyword found in resume>", ...],
  "keywords_missing": ["<important keyword absent from resume>", ...]
}

Be specific, honest, and constructive. Focus on substance over formatting."""


# ── helpers ────────────────────────────────────────────────────────────────────


def extract_pdf_text(data: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


async def assert_public_url(url: str) -> None:
    """Reject non-http(s) URLs and hosts that resolve to private/internal addresses (SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=422, detail="Job posting URL must be an http(s) URL")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or None)
    except OSError:
        raise HTTPException(status_code=422, detail="Could not resolve job posting host") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise HTTPException(status_code=422, detail="Job posting URL must point to a public host")


async def fetch_job_posting(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        # Follow redirects manually so every hop is checked against the SSRF guard
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                await assert_public_url(url)
                response = await client.get(url, headers=headers)
                if not response.is_redirect:
                    break
                url = urljoin(url, response.headers["location"])
            else:
                raise HTTPException(status_code=422, detail="Job posting URL redirected too many times")
            response.raise_for_status()
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=422, detail="Job posting URL timed out") from None
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=422, detail=f"Job posting URL returned {exc.response.status_code}") from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not fetch job posting: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _analyze_groq(content: str, groq_key: str) -> dict:
    # The signed-in user's own key — the server never spends its own LLM quota
    client = Groq(api_key=groq_key)
    try:
        response = client.chat.completions.create(
            model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            max_tokens=4096,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": GROQ_SYSTEM},
                {"role": "user", "content": content},
            ],
        )
    except AuthenticationError:
        raise HTTPException(
            status_code=400, detail="Groq rejected your saved key — update it in Settings (/settings)"
        ) from None
    except APIError as exc:
        raise HTTPException(status_code=502, detail=f"Groq model unavailable: {exc.message}") from exc
    try:
        return json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, IndexError, TypeError):
        raise HTTPException(status_code=500, detail="Groq model returned an unexpected response format") from None


# ── models ─────────────────────────────────────────────────────────────────────


class AnalysisResult(BaseModel):
    match_score: int
    verdict: str
    strengths: list[str]
    gaps: list[str]
    suggestions: list[str]
    keywords_matched: list[str]
    keywords_missing: list[str]


# ── routes ─────────────────────────────────────────────────────────────────────


# Same for every visitor (personal data loads via JS), so serve it from Vercel's regional edge cache.
# Browsers always revalidate; the edge refreshes in the background.
PAGE_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=0, must-revalidate",
    "Vercel-CDN-Cache-Control": "max-age=300, stale-while-revalidate=86400",
}


@app.get("/resume-analyzer", response_class=HTMLResponse)
async def ui(response: Response) -> str:
    response.headers.update(PAGE_CACHE_HEADERS)
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Resume Analyzer · Bifrost</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0a0a0a;color:#e5e5e5;min-height:100vh;padding:48px 24px}
    .container{max-width:720px;margin:0 auto}
    header{margin-bottom:40px}
    h1{font-size:2rem;font-weight:700;background:linear-gradient(135deg,#6366f1,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
    .subtitle{margin-top:8px;color:#737373;font-size:.95rem}
    .card{background:#141414;border:1px solid #262626;border-radius:12px;padding:28px;margin-bottom:20px}
    label{display:block;font-size:.85rem;font-weight:500;color:#a3a3a3;margin-bottom:8px}
    input[type=text]{width:100%;background:#0a0a0a;border:1px solid #333;border-radius:8px;padding:10px 14px;color:#e5e5e5;font-size:.95rem;outline:none;transition:border-color .2s}
    input[type=text]:focus{border-color:#6366f1}
    .drop-zone{border:2px dashed #333;border-radius:8px;padding:32px;text-align:center;cursor:pointer;transition:border-color .2s,background .2s;position:relative}
    .drop-zone:hover,.drop-zone.drag-over{border-color:#6366f1;background:#1a1a2e}
    .drop-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
    .drop-zone .icon{font-size:2rem;margin-bottom:8px}
    .drop-zone p{color:#737373;font-size:.9rem}
    .drop-zone .file-name{color:#a78bfa;font-size:.9rem;margin-top:6px;font-weight:500}
    button{width:100%;padding:12px;background:linear-gradient(135deg,#6366f1,#a78bfa);border:none;border-radius:8px;color:#fff;font-size:1rem;font-weight:600;cursor:pointer;transition:opacity .2s}
    button:hover{opacity:.9}
    button:disabled{opacity:.5;cursor:not-allowed}
    .spinner{display:none;text-align:center;padding:32px;color:#737373}
    .spinner.show{display:block}
    .dot{display:inline-block;animation:bounce 1.2s infinite;margin:0 2px;font-size:1.5rem}
    .dot:nth-child(2){animation-delay:.2s}
    .dot:nth-child(3){animation-delay:.4s}
    @keyframes bounce{0%,80%,100%{transform:translateY(0)}40%{transform:translateY(-8px)}}
    #results{display:none}
    #results.show{display:block}
    .score-row{display:flex;align-items:center;gap:24px;margin-bottom:20px}
    .score-circle{width:88px;height:88px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;flex-shrink:0;font-weight:700}
    .score-num{font-size:1.8rem;line-height:1}
    .score-label{font-size:.65rem;color:rgba(255,255,255,.7);margin-top:2px}
    .verdict{font-size:1.05rem;color:#e5e5e5}
    .section{margin-bottom:20px}
    .section-title{font-size:.8rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}
    .green{color:#4ade80}
    .red{color:#f87171}
    .yellow{color:#fbbf24}
    .purple{color:#a78bfa}
    ul{list-style:none;display:flex;flex-direction:column;gap:6px}
    li{font-size:.9rem;color:#d4d4d4;padding-left:16px;position:relative}
    li::before{content:"·";position:absolute;left:4px;color:#525252}
    .tags{display:flex;flex-wrap:wrap;gap:6px}
    .tag{font-size:.78rem;padding:3px 10px;border-radius:999px;font-weight:500}
    .tag-green{background:#14532d;color:#4ade80}
    .tag-red{background:#450a0a;color:#f87171}
    .error{background:#1a0a0a;border:1px solid #7f1d1d;border-radius:8px;padding:16px;color:#fca5a5;font-size:.9rem;display:none}
    .error.show{display:block}
    .gate { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 16px; padding: 14px 16px; border-radius: 12px; border: 1px solid #fbbf24; background: rgba(251, 191, 36, .1); font-size: .92rem; }
    .gate[hidden] { display: none; }
    .gate a.cta { background: #24292f; color: #fff; padding: 8px 14px; border-radius: 8px; font-weight: 600; text-decoration: none; white-space: nowrap; }
    footer{margin-top:48px;text-align:center;color:#525252;font-size:.85rem}
    footer a{color:#6366f1;text-decoration:none}
  </style>
</head>
<body>
<div class="container">
  <header>
    <h1>Resume Analyzer</h1>
    <p class="subtitle">Upload your resume PDF and paste a job posting URL to get an AI-powered match analysis.</p>
  </header>

  <div class="gate" id="gate" role="status" hidden></div>

  <div class="card">
    <div style="margin-bottom:20px">
      <label>Resume (PDF)</label>
      <div class="drop-zone" id="dropZone">
        <input type="file" id="resumeFile" accept=".pdf"/>
        <div class="icon">📄</div>
        <p>Drag & drop your PDF here or <strong>click to browse</strong></p>
        <p class="file-name" id="fileName"></p>
      </div>
    </div>
    <div style="margin-bottom:24px">
      <label for="jobUrl">Job Posting URL</label>
      <input type="text" id="jobUrl" placeholder="https://jobs.example.com/software-engineer"/>
    </div>
    <button id="analyzeBtn" onclick="analyze()">Analyze Match</button>
  </div>

  <div class="error" id="errorBox"></div>

  <div class="spinner" id="spinner">
    <p style="margin-bottom:12px;color:#a3a3a3">Analyzing your resume against the job posting…</p>
    <span class="dot">·</span><span class="dot">·</span><span class="dot">·</span>
  </div>

  <div id="results">
    <div class="card">
      <div class="score-row">
        <div class="score-circle" id="scoreCircle">
          <span class="score-num" id="scoreNum">-</span>
          <span class="score-label">/ 100</span>
        </div>
        <p class="verdict" id="verdict"></p>
      </div>

      <div class="section">
        <p class="section-title green">✦ Strengths</p>
        <ul id="strengthsList"></ul>
      </div>
      <div class="section">
        <p class="section-title red">✦ Gaps</p>
        <ul id="gapsList"></ul>
      </div>
      <div class="section">
        <p class="section-title yellow">✦ Suggestions</p>
        <ul id="suggestionsList"></ul>
      </div>
      <div class="section">
        <p class="section-title green">✦ Keywords Matched</p>
        <div class="tags" id="matchedTags"></div>
      </div>
      <div class="section" style="margin-bottom:0">
        <p class="section-title red">✦ Keywords Missing</p>
        <div class="tags" id="missingTags"></div>
      </div>
    </div>
  </div>

  <footer><p><a href="/">← Back to Bifrost API</a></p></footer>
</div>

<script>
  const dropZone = document.getElementById('dropZone');
  const fileInput = document.getElementById('resumeFile');
  const fileName  = document.getElementById('fileName');

  fileInput.addEventListener('change', () => {
    fileName.textContent = fileInput.files[0]?.name || '';
  });
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
  dropZone.addEventListener('drop', e => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file) {
      const dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
      fileName.textContent = file.name;
    }
  });

  function scoreColor(n) {
    if (n >= 75) return '#4ade80';
    if (n >= 50) return '#fbbf24';
    return '#f87171';
  }

  // Model output is derived from untrusted job pages — build nodes with textContent, never innerHTML
  function renderItems(id, items, tagName, className) {
    const el = document.getElementById(id);
    el.replaceChildren(...items.map(t => {
      const node = document.createElement(tagName);
      if (className) node.className = className;
      node.textContent = t;
      return node;
    }));
  }

  function renderList(id, items) {
    renderItems(id, items, 'li');
  }

  function renderTags(id, items, cls) {
    renderItems(id, items, 'span', `tag ${cls}`);
  }

  // Runs on the user's own Groq key: sign-in plus a saved key are required before analysing
  const gate = document.getElementById('gate');
  function setGate(state) {
    const btn = document.getElementById('analyzeBtn');
    if (state === 'ok') { gate.hidden = true; btn.disabled = false; return; }
    const signin = state === 'signed-out';
    gate.replaceChildren(
      document.createTextNode(signin
        ? 'Sign in with GitHub to use this app — it runs on your own Groq API key.'
        : 'Add your Groq API key in Settings to use this app — usage is billed to your own Groq account.'),
      Object.assign(document.createElement('a'), {
        className: 'cta',
        href: signin ? '/auth/login?next=/resume-analyzer' : '/settings',
        textContent: signin ? 'Sign in with GitHub' : 'Open Settings',
      }));
    gate.hidden = false;
    btn.disabled = true;
  }
  async function checkAccess() {
    try {
      const [me, keys] = await Promise.all([fetch('/auth/me'), fetch('/auth/keys')]);
      if (me.status === 401) return setGate('signed-out');
      const list = keys.ok ? await keys.json() : [];
      setGate(list.some(k => k.provider === 'groq' && k.configured) ? 'ok' : 'no-key');
    } catch { /* leave the form usable; the request itself will explain */ }
  }
  // A request can still be refused (expired session, key removed elsewhere)
  function gateFromStatus(status) {
    if (status === 401) { setGate('signed-out'); return true; }
    if (status === 428) { setGate('no-key'); return true; }
    return false;
  }
  checkAccess();

  async function analyze() {
    const file = fileInput.files[0];
    const url  = document.getElementById('jobUrl').value.trim();
    const err  = document.getElementById('errorBox');
    err.classList.remove('show');

    if (!file) { err.textContent = 'Please select a PDF file.'; err.classList.add('show'); return; }
    if (!url)  { err.textContent = 'Please enter a job posting URL.'; err.classList.add('show'); return; }

    document.getElementById('analyzeBtn').disabled = true;
    document.getElementById('spinner').classList.add('show');
    document.getElementById('results').classList.remove('show');

    const form = new FormData();
    form.append('resume', file);
    form.append('job_url', url);

    try {
      const res  = await fetch('/resume-analyzer/analyze', { method: 'POST', body: form });
      const data = await res.json();
      if (gateFromStatus(res.status)) return;
      if (!res.ok) { throw new Error(data.detail || 'Analysis failed'); }

      const circle = document.getElementById('scoreCircle');
      const color  = scoreColor(data.match_score);
      circle.style.background = `conic-gradient(${color} ${data.match_score}%, #262626 0)`;
      document.getElementById('scoreNum').textContent  = data.match_score;
      document.getElementById('verdict').textContent   = data.verdict;
      renderList('strengthsList',  data.strengths);
      renderList('gapsList',       data.gaps);
      renderList('suggestionsList', data.suggestions);
      renderTags('matchedTags', data.keywords_matched, 'tag-green');
      renderTags('missingTags', data.keywords_missing, 'tag-red');

      document.getElementById('results').classList.add('show');
    } catch (e) {
      err.textContent = e.message;
      err.classList.add('show');
    } finally {
      document.getElementById('analyzeBtn').disabled = !gate.hidden;
      document.getElementById('spinner').classList.remove('show');
    }
  }
</script>
</body>
</html>"""


@app.get("/resume-analyzer/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/resume-analyzer/analyze", response_model=AnalysisResult)
async def analyze(
    resume: UploadFile = File(..., description="Resume as a PDF file"),
    job_url: str = Form(..., description="URL of the job posting"),
    groq_key: str = Depends(auth.require_key("groq")),
) -> dict:
    # ── validate resume ──────────────────────────────────────────────────────
    if not (resume.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    pdf_bytes = await resume.read()
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise HTTPException(status_code=400, detail="PDF exceeds the 4 MB limit")

    try:
        resume_text = extract_pdf_text(pdf_bytes)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse PDF: {exc}") from exc

    if not resume_text.strip():
        raise HTTPException(status_code=422, detail="No text could be extracted from the PDF (scanned image?)")

    # ── fetch job posting ────────────────────────────────────────────────────
    job_text = await fetch_job_posting(job_url)
    if not job_text.strip():
        raise HTTPException(status_code=422, detail="No readable text found at the job posting URL")

    # ── run analysis with the user's Groq key ────────────────────────────────
    content = f"## Resume\n\n{resume_text[:MAX_RESUME_CHARS]}\n\n## Job Posting\n\n{job_text[:MAX_JOB_CHARS]}"
    return await run_in_threadpool(_analyze_groq, content, groq_key)
