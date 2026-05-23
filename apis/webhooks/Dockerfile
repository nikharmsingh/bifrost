FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages/core  ./packages/core
COPY apis/webhooks  ./apis/webhooks

RUN pip install uv && \
    uv sync --package webhooks --frozen --no-dev

EXPOSE 8000

CMD ["uv", "run", "--package", "webhooks", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
