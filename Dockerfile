# Debian 12: the newest distro Playwright 1.49 installs Chromium dependencies for.
FROM python:3.12-slim-bookworm

ENV TZ=Europe/Kyiv \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app/ ./app/

CMD ["python", "-m", "app"]
