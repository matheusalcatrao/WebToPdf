# ── Base image ────────────────────────────────────────────────────────────────
FROM --platform=linux/amd64 python:3.11-slim

# ── System deps required by Chrome ────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    fonts-liberation libasound2 libatk-bridge2.0-0 \
    libatk1.0-0 libcairo2 libcups2 libdbus-1-3 libdrm2 libexpat1 \
    libgbm1 libglib2.0-0 libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 \
    libpangocairo-1.0-0 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxext6 libxfixes3 libxi6 libxkbcommon0 \
    libxrandr2 libxrender1 libxss1 libxtst6 xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# ── Install Google Chrome stable via official apt repo ────────────────────────
RUN wget -q -O /usr/share/keyrings/google-chrome.gpg \
    https://dl.google.com/linux/linux_signing_key.pub \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
    http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App source ────────────────────────────────────────────────────────────────
COPY . .

# ── Runtime config ────────────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1
# Suppress webdriver-manager download logs
ENV WDM_LOG=0
# Cache ChromeDriver in /tmp (writable in any container)
ENV WDM_CACHE_PATH=/tmp/.wdm

EXPOSE 8080

# 1 process · uvicorn worker · 10-min timeout (scraping can be slow)
# Single worker is intentional: in-process job lock (_job_lock) enforces
# one-at-a-time semantics; multiple workers would bypass it across processes.
CMD gunicorn --workers 1 --worker-class uvicorn.workers.UvicornWorker --timeout 600 --bind 0.0.0.0:${PORT:-8080} app:app
