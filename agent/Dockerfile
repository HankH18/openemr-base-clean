# Minimal production image for `copilot-api`.
#
# Multi-stage: builder installs the pinned deps into a venv, runtime is
# python:3.12-slim with only that venv copied in. Non-root user for HIPAA
# posture.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN pip install uv==0.11.8

COPY pyproject.toml README.md ./
COPY copilot ./copilot
COPY migrations ./migrations
COPY alembic.ini ./

RUN uv venv /opt/venv --python 3.12
ENV PATH="/opt/venv/bin:$PATH"
RUN uv pip install --no-cache -e .


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# tesseract OCR system binary — local, in-container OCR for document ingestion
# (word-box extraction). PHI never leaves the deployment for bounding boxes.
# tesseract-ocr-eng ships the English trained data used by the default OCR lang.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash --uid 10001 copilot

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
RUN chown -R copilot:copilot /app

USER copilot

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "copilot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
