# ORIGIN: AI — drafted by Claude Code, reviewed by Kiel
FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# pyproject.toml declares readme = "README.md", so the build needs it too.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "clinical_context.main:app", "--host", "0.0.0.0", "--port", "8000"]
