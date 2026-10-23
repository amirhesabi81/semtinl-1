# syntax=docker/dockerfile:1.7
# Direct-source runtime; quality validated before packaging.
FROM python:3.12-slim-bookworm AS system-dependencies
RUN set -eu; \
    installed=0; \
    for mirror in default https alternate; do \
      if [ "$mirror" = https ]; then \
        sed -i 's#http://deb.debian.org/#https://deb.debian.org/#g' /etc/apt/sources.list.d/debian.sources; \
      elif [ "$mirror" = alternate ]; then \
        sed -i 's#https://deb.debian.org/debian-security#https://security.debian.org/debian-security#g; s#https://deb.debian.org/debian#https://ftp.debian.org/debian#g' /etc/apt/sources.list.d/debian.sources; \
      fi; \
      echo "SENTINEL Debian system dependencies: trying $mirror mirror"; \
      if apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update \
        && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
          adduser ca-certificates fonts-dejavu-core fonts-noto-core gosu nodejs postgresql-client tzdata; then \
        installed=1; break; \
      fi; \
      rm -rf /var/lib/apt/lists/*; \
      sleep 3; \
    done; \
    if [ "$installed" -ne 1 ]; then echo 'SENTINEL: apt dependencies unavailable after all 3 Debian mirrors' >&2; exit 1; fi; \
    test -s /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf; \
    test -s /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf; \
    node --version; \
    command -v gosu; \
    command -v psql; \
    rm -rf /var/lib/apt/lists/*

FROM system-dependencies AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    PORT=8080 \
    TZ=Asia/Tehran
WORKDIR /app
# Keep the quality stage cached; install the matching backup client only in
# runtime. Debian Bookworm's default pg_dump 15 cannot dump Railway Postgres 18.
# Pin the official PGDG signing key bytes and use apt's signed repository.
RUN python -c 'from pathlib import Path; from urllib.request import urlopen; import hashlib; key=urlopen("https://www.postgresql.org/media/keys/ACCC4CF8.asc", timeout=30).read(); assert hashlib.sha256(key).hexdigest() == "0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76"; Path("/usr/share/keyrings/pgdg.asc").write_bytes(key); Path("/etc/apt/sources.list.d/pgdg.list").write_text("deb [signed-by=/usr/share/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main\n")' \
 && apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 update \
 && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 install -y --no-install-recommends postgresql-client-18 \
 && pg_dump --version \
 && pg_dump --version | grep -E 'PostgreSQL\) 18\.' \
 && pg_restore --version | grep -E 'PostgreSQL\) 18\.' \
 && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    addgroup --system sentinel; \
    adduser --system --ingroup sentinel --home /app --no-create-home sentinel
COPY requirements.lock.txt /tmp/requirements.lock.txt
# Railway Metal supports cache mounts only; transfer the verified wheels with COPY.
RUN python -m pip install --retries 4 --timeout 60 --require-hashes --only-binary=:all: -r /tmp/requirements.lock.txt \
 && python -m pip check \
 && rm -rf /opt/sentinel-wheels \
 && rm -f /tmp/requirements.lock.txt
COPY . /app
RUN python -m compileall -q main.py bot.py config.py bot_runtime core modules services repositories handlers jobs ui scripts \
 && python main.py --check \
 && rm -rf /app/tests /app/.pytest_cache /app/.ruff_cache /app/.mypy_cache /app/.coverage /app/.coverage.* \
              /app/quality-artifacts \
              /app/requirements-dev.txt /app/requirements-dev.lock.txt /app/requirements-dev.in \
              /app/Dockerfile.quality /app/Dockerfile.quality.dockerignore \
 && rm -f /app/sentinel_runtime.zip /app/sentinel_runtime.zip.sha256 /app/restore_runtime.py \
 && mkdir -p /app/data/backups /app/data/payment_receipts /app/data/support_attachments \
              /app/data/signal_templates /app/logs \
 && chmod 0755 /app/scripts/docker-entrypoint.sh \
 && chown -R sentinel:sentinel /app
EXPOSE 8080
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
  CMD ["python", "scripts/healthcheck.py", "--mode", "http", "--timeout", "5"]
CMD ["python", "main.py"]
