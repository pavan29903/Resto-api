#!/usr/bin/env sh
# Container entrypoint.
#
# Fly runs migrations as a release_command, which is better — a failed
# migration aborts the deploy instead of crash-looping. Render's free plan has
# no such hook, so it sets MIGRATE_ON_START=1 and we run them here instead.
# Alembic is idempotent, so running twice is harmless.

set -e

if [ "${MIGRATE_ON_START:-0}" = "1" ]; then
  echo "==> running database migrations"
  alembic upgrade head
fi

# PORT is injected by the platform (Render sets it; Fly uses fly.toml).
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8080}" \
  --workers 1
