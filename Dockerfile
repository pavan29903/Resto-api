# RestoFood API — built remotely by Render or Fly.io, so no local Docker needed.
#
# Python 3.11 rather than 3.14: every dependency here ships prebuilt wheels for
# 3.11, which keeps the image small and the build fast (no compiler needed).

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is used by the container healthcheck; nothing else is needed at runtime
# because all our dependencies ship wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, in their own layer: application code changes far more
# often than the dependency set, so this layer stays cached between deploys.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY entrypoint.sh ./
# Strip CRLF: the file is authored on Windows, and a trailing carriage return
# makes the shebang unrunnable on Linux ("no such file or directory").
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# Run as a non-root user — if the app is ever compromised, it shouldn't own
# the filesystem it's running on.
RUN useradd --create-home --uid 1000 restofood && chown -R restofood:restofood /app
USER restofood

EXPOSE 8080

# Generous start period: on a cold start the container also runs migrations
# before the app answers, and a healthcheck failing during that would restart
# the container in a loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8080}/health" || exit 1

# One worker per machine. Scale with more machines rather than more workers —
# each worker holds its own database connection pool, and Supabase's free tier
# has a modest connection cap.
CMD ["./entrypoint.sh"]
