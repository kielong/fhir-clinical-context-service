# ORIGIN: AI — drafted by Claude Code, reviewed by Kiel
FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The exact versions that were tested come first, in a layer of their own: it is rebuilt only when
# requirements.lock changes, not on every edit to the code. (Re-pin with `make lock`.)
COPY requirements.lock ./
RUN pip install -r requirements.lock

# pyproject.toml declares readme = "README.md", so the build needs it too. The dependencies are
# already installed, so nothing is resolved again here.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "clinical_context.main:app", "--host", "0.0.0.0", "--port", "8000"]
