from .base import *  # noqa: F401,F403
from .base import env

DEBUG = env.bool("DEBUG", default=True)

# Run without Redis: USE_LOCMEM_CACHE=True and CELERY_TASK_ALWAYS_EAGER=True in .env.
if env.bool("USE_LOCMEM_CACHE", default=False):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
