#!/bin/sh
# Usage: docker-entrypoint.sh web | worker | <any command>
set -e

case "$1" in
  web)
    python manage.py migrate --noinput
    # Seed the document types only once, so edits made in the admin survive restarts.
    if ! python manage.py shell -c "import sys; from documents.models import DocumentType; sys.exit(0 if DocumentType.objects.exists() else 1)"; then
      python manage.py loaddata document_types
    fi
    if [ "${USE_AZURE_STORAGE:-False}" = "True" ]; then
      python manage.py init_storage
    fi
    # Free hosts (e.g. Render) allow only one service, so the Celery worker can run in the same container.
    if [ "${RUN_WORKER_IN_WEB:-False}" = "True" ]; then
      celery -A config worker -l "${CELERY_LOG_LEVEL:-info}" --concurrency "${CELERY_CONCURRENCY:-1}" &
    fi
    exec gunicorn config.wsgi:application \
      --bind "0.0.0.0:${PORT:-8000}" \
      --workers "${GUNICORN_WORKERS:-3}" \
      --threads "${GUNICORN_THREADS:-2}" \
      --timeout "${GUNICORN_TIMEOUT:-120}" \
      --access-logfile -
    ;;
  worker)
    exec celery -A config worker -l "${CELERY_LOG_LEVEL:-info}" --concurrency "${CELERY_CONCURRENCY:-2}"
    ;;
  *)
    exec "$@"
    ;;
esac
