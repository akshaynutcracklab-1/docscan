import tempfile

from .base import *  # noqa: F401,F403

DEBUG = False
SECRET_KEY = "test-secret-key-that-is-long-enough-for-hs256"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MEDIA_ROOT = tempfile.mkdtemp(prefix="docscan-test-media-")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
USE_AZURE_STORAGE = False

CELERY_TASK_ALWAYS_EAGER = True
# Retry() must not escape eagerly-run tasks (a real worker swallows it too).
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_BROKER_URL = "memory://"

# Tests never touch Azure: the mock engine is the default, the azure path is tested with a fake client.
OCR_ENGINE = "mock"
DOCINTEL_ENDPOINT = "https://example.cognitiveservices.azure.com/"
DOCINTEL_KEY = "test-key"
DOCINTEL_TIER = "F0"
DOCINTEL_RATE_LIMIT = "1000/s"
PIPELINE_RETRY_BACKOFF = 0
