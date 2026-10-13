FROM python:3.14-slim AS cli

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.21
COPY .python-version pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
RUN useradd --create-home --uid 10001 app
USER app
CMD ["/app/.venv/bin/bront", "--help"]

FROM cli AS reference
USER root
RUN uv sync --frozen --no-dev --no-editable --extra reference
USER app
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "bront.reference.gateway:app", "--host", "0.0.0.0", "--port", "8000"]
