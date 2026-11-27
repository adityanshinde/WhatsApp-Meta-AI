FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir -e . && \
    playwright install chromium --with-deps

ENV WA_HOST=0.0.0.0 \
    WA_PORT=8000 \
    WA_HEADLESS=true \
    WA_PROFILE_DIR=/app/wa_profile \
    WA_DOWNLOADS_DIR=/app/downloads

EXPOSE 8000

CMD ["wa-meta-ai", "serve", "--host", "0.0.0.0", "--port", "8000", "--headless"]
