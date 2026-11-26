FROM python:3.12-slim

# Set by the build (see .github/workflows/docker.yml). Shown in the dashboard footer.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/cards.db \
    TZ=America/New_York \
    NTFY_LISTEN_HTTP=:2586 \
    NTFY_CACHE_FILE=/data/ntfy/cache.db \
    NTFY_AUTH_FILE=/data/ntfy/auth.db \
    NTFY_AUTH_DEFAULT_ACCESS=deny-all \
    NTFY_ENABLE_LOGIN=true

# The ntfy server runs in this same container. Its program is copied from the official image.
COPY --from=binwiederhier/ntfy:latest /usr/bin/ntfy /usr/local/bin/ntfy

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ntfy_accounts.py entrypoint.sh ./
# Files saved on Windows can carry CRLF line endings, which break the shell script.
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

VOLUME /data
EXPOSE 8080 2586

CMD ["./entrypoint.sh"]
