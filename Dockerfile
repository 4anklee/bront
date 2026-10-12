FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.21
COPY .python-version pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "inference_guard.gateway:app", "--host", "0.0.0.0", "--port", "8000"]
