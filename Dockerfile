FROM python:3.12-slim-bookworm

# Tesseract with the three language packs the UI offers (English, French, Arabic).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-ara \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/drive-to-text

COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 runner \
    && chown -R runner:runner /srv/drive-to-text

USER runner
ENV HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/capabilities')"

CMD ["python", "-m", "app.main"]
