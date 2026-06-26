#!/usr/bin/env sh
set -e

# Assemble DATABASE_URL from parts when not supplied directly.
# Kubernetes/Helm passes DB_* (password via secret); Docker Compose passes
# DATABASE_URL directly, so this block no-ops there.
if [ -z "${DATABASE_URL:-}" ] && [ -n "${DB_HOST:-}" ]; then
  export DATABASE_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT:-5432}/${DB_NAME}"
fi

migrate() { alembic upgrade head; }
serve()   { exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers; }

case "${1:-all}" in
  migrate)   migrate ;;
  serve)     serve ;;
  all)       migrate; serve ;;
  await-db)  until alembic current 2>/dev/null | grep -q '(head)'; do echo "waiting for DB migrations..."; sleep 3; done ;;
  *)         exec "$@" ;;
esac
