"""Remove files from storage when rows are deleted (also fires for cascade deletes)."""
import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Document, Page

logger = logging.getLogger(__name__)


def delete_file(field_file) -> None:
    if not field_file or not field_file.name:
        return
    try:
        field_file.storage.delete(field_file.name)
    except Exception:  # storage hiccups must not break the delete request
        logger.warning("Could not delete file %s from storage", field_file.name, exc_info=True)


@receiver(post_delete, sender=Page)
def delete_page_files(sender, instance: Page, **kwargs):
    delete_file(instance.image)
    delete_file(instance.page_pdf)


@receiver(post_delete, sender=Document)
def delete_document_files(sender, instance: Document, **kwargs):
    delete_file(instance.pdf_file)
