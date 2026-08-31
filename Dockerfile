# MenuSnap API — built remotely by Fly.io, so no local Docker needed.
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

# Run as a non-root user — if the app is ever compromised, it shouldn't own
# the filesystem it's running on.
RUN useradd --create-home --uid 1000 menusnap && chown -R menusnap:menusnap /app
USER menusnap

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8080/health || exit 1

# One worker per small Fly machine. Scale with more machines rather than more
# workers — each worker holds its own database connection pool, and Supabase's
# free tier has a modest connection cap.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
