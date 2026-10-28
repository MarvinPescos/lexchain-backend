FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OCR_WORKERS=2

WORKDIR /app

RUN addgroup --gid 1000 appgroup && \
    adduser --uid 1000 --gid 1000 --home /home/appuser --shell /bin/bash --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade --retries 10 --timeout 120 -r requirements.txt

COPY --chown=appuser:appgroup ocr_service ./ocr_service

USER appuser

# Surya runs in its own vLLM container; set SURYA_INFERENCE_URL to reach it.
CMD ["uvicorn", "ocr_service.server:app", "--host", "0.0.0.0", "--port", "8001"]
