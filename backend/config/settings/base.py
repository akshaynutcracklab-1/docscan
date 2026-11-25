"""Base settings shared by every environment. Secrets come from `.env`."""
from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    USE_AZURE_STORAGE=(bool, False),
)
# backend/.env wins; the repository-root .env is read as a fallback (it never overrides).
for _candidate in (BASE_DIR / ".env", BASE_DIR.parent / ".env"):
    if _candidate.exists():
        environ.Env.read_env(_candidate)

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # third party
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "drf_spectacular",
    "django_celery_results",
    # local
    "accounts",
    "documents",
    "processing",
    "scanner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    _db_name = Path(DATABASES["default"]["NAME"])
    if not _db_name.is_absolute():  # "sqlite:///db.sqlite3" -> backend/db.sqlite3, whatever the cwd
        DATABASES["default"]["NAME"] = str(BASE_DIR / _db_name)
    DATABASES["default"]["OPTIONS"] = {
        # The API and the Celery worker write concurrently: WAL lets readers proceed during writes,
        # IMMEDIATE takes the write lock at BEGIN (serialising submit/upload transactions, which also
        # makes the status checks safe without row locks), and timeout waits instead of "database is locked".
        "timeout": 20,
        "transaction_mode": "IMMEDIATE",
        "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=20000;",
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", default=str(BASE_DIR / "media")))
FIXTURE_DIRS = [BASE_DIR / "seed"]
# Serve MEDIA_ROOT through Django even with DEBUG=False (single-host Docker setup without Azure storage).
# Paths contain the user id and an unguessable document UUID, but are not access-checked.
SERVE_MEDIA = env.bool("SERVE_MEDIA", default=False)

# ---------------------------------------------------------------- storage
USE_AZURE_STORAGE = env("USE_AZURE_STORAGE")
# Azurite's documented, public development account (not a secret).
AZURITE_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)
AZURE_STORAGE_CONNECTION_STRING = env("AZURE_STORAGE_CONNECTION_STRING", default="UseDevelopmentStorage=true")
if AZURE_STORAGE_CONNECTION_STRING.strip().lower() == "usedevelopmentstorage=true":
    # Expanded so django-storages has an account key to sign SAS URLs with.
    AZURE_STORAGE_CONNECTION_STRING = AZURITE_CONNECTION_STRING
AZURE_STORAGE_CONTAINER = env("AZURE_STORAGE_CONTAINER", default="documents")
SIGNED_URL_EXPIRY_SECONDS = env.int("SIGNED_URL_EXPIRY_SECONDS", default=15 * 60)

if USE_AZURE_STORAGE:
    _default_storage = {
        "BACKEND": "storages.backends.azure_storage.AzureStorage",
        "OPTIONS": {
            "connection_string": AZURE_STORAGE_CONNECTION_STRING,
            "azure_container": AZURE_STORAGE_CONTAINER,
            # url() returns a read-only SAS URL valid for this many seconds.
            "expiration_secs": SIGNED_URL_EXPIRY_SECONDS,
            "overwrite_files": False,
        },
    }
else:
    _default_storage = {"BACKEND": "django.core.files.storage.FileSystemStorage"}

STORAGES = {
    "default": _default_storage,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Uploaded pages larger than this are spooled to disk instead of memory.
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
MAX_PAGE_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_BULK_UPLOAD_FILES = 50

# ---------------------------------------------------------------- DRF / auth
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ("rest_framework_simplejwt.authentication.JWTAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.AnonRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "user": env("THROTTLE_USER", default="120/min"),
        "anon": env("THROTTLE_ANON", default="30/min"),
        "uploads": env("THROTTLE_UPLOADS", default="60/min"),
    },
    # `?format=` belongs to the download endpoint (pdf|txt|json), not to content negotiation.
    "URL_FORMAT_OVERRIDE": None,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=30)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=7)),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "DocScan API",
    "DESCRIPTION": "Document scanning backend: upload pages, OCR with Azure AI Document Intelligence, "
    "searchable PDFs and type-specific field extraction.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "DocumentStatusEnum": "documents.models.Document.Status",
        "PageStatusEnum": "documents.models.Page.Status",
    },
}

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:5173"])

# ---------------------------------------------------------------- cache / celery
REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = "django-db"
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TIMEZONE = TIME_ZONE

# ---------------------------------------------------------------- OCR
OCR_ENGINE = env("OCR_ENGINE", default="azure").lower()  # azure | mock
DOCINTEL_ENDPOINT = env("DOCINTEL_ENDPOINT", default="")
DOCINTEL_KEY = env("DOCINTEL_KEY", default="")
DOCINTEL_TIER = env("DOCINTEL_TIER", default="F0").upper()  # F0 | S0
DOCINTEL_RATE_LIMIT = env("DOCINTEL_RATE_LIMIT", default="20/m")
DOCINTEL_DETECT_LANGUAGE = env.bool("DOCINTEL_DETECT_LANGUAGE", default=True)
DOCINTEL_POLL_TIMEOUT = env.int("DOCINTEL_POLL_TIMEOUT", default=300)
LOW_CONFIDENCE_THRESHOLD = env.float("LOW_CONFIDENCE_THRESHOLD", default=0.80)
PIPELINE_MAX_RETRIES = 3
PIPELINE_RETRY_BACKOFF = env.int("PIPELINE_RETRY_BACKOFF", default=5)  # seconds, doubled per retry

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # The Azure HTTP logger prints request details; keep it at WARNING so keys never reach logs.
        "azure": {"level": "WARNING"},
    },
}
