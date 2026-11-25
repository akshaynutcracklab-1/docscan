from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "documents"

    def ready(self):
        from pillow_heif import register_heif_opener

        register_heif_opener()  # lets Pillow (and ImageField) read HEIC/HEIF uploads
        from . import signals  # noqa: F401
