FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages/core  ./packages/core
COPY packages/tools ./packages/tools
COPY mcp-servers/knowledge-base ./mcp-servers/knowledge-base

RUN pip install uv && \
    uv sync --package knowledge-base --frozen --no-dev

EXPOSE 8000

CMD ["uv", "run", "--package", "knowledge-base", "python", "src/server.py"]
