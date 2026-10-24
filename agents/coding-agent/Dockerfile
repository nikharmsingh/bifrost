FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages/core   ./packages/core
COPY packages/llm    ./packages/llm
COPY agents/coding-agent ./agents/coding-agent

RUN pip install uv && \
    uv sync --package coding-agent --frozen --no-dev

CMD ["uv", "run", "--package", "coding-agent", "python", "-m", "src.main"]
