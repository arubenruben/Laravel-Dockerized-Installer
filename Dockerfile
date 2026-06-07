# HuggingFace Spaces Dockerfile
# Mirrors api/Dockerfile but runs as a non-root user (UID 1000) as required by HF Spaces.
# The build context is the repo root, so api/ contents are copied explicitly.

FROM python:3.13-slim

# ── System packages: PHP 8.2, Composer, Node.js 20 ─────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git unzip zip ca-certificates gnupg \
        php8.2-cli php8.2-mbstring php8.2-xml php8.2-curl php8.2-zip \
        php8.2-mysql php8.2-pgsql php8.2-sqlite3 php8.2-gd php8.2-bcmath \
    && rm -rf /var/lib/apt/lists/*

# ── Composer ────────────────────────────────────────────────────────────────
RUN curl -sS https://getcomposer.org/installer \
    | php -- --install-dir=/usr/local/bin --filename=composer

# ── Node.js 20 ───────────────────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Laravel installer ────────────────────────────────────────────────────────
RUN composer global require laravel/installer --no-interaction -q

# ── Non-root user (required by HuggingFace Spaces) ──────────────────────────
RUN useradd -m -u 1000 user

USER user

ENV PATH="/home/user/.local/bin:/root/.config/composer/vendor/bin:/root/.composer/vendor/bin:$PATH"

# ── Python API ───────────────────────────────────────────────────────────────
WORKDIR /app

COPY --chown=user api/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user api/ .

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
