# DocScan backend: one image for both the API (gunicorn) and the Celery worker.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --create-home --uid 1000 app

# Dependencies first so code changes don't reinstall them.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Strip Windows line endings from the entrypoint, collect static files for whitenoise,
# and create /data (SQLite database + uploaded files) owned by the app user so the volume inherits it.
RUN sed -i 's/\r$//' docker-entrypoint.sh \
 && chmod +x docker-entrypoint.sh \
 && SECRET_KEY=build-only DJANGO_SETTINGS_MODULE=config.settings.prod python manage.py collectstatic --noinput \
 && mkdir -p /data/media \
 && chown -R app:app /data /app/staticfiles

USER app

ENV DJANGO_SETTINGS_MODULE=config.settings.prod \
    DATABASE_URL=sqlite:////data/db.sqlite3 \
    MEDIA_ROOT=/data/media

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz/', timeout=4).status == 200 else 1)" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["web"]
