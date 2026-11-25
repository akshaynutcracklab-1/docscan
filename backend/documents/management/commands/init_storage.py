from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the Azure Blob container (Azure or Azurite) if it does not exist yet."

    def handle(self, *args, **options):
        if not settings.USE_AZURE_STORAGE:
            self.stdout.write("USE_AZURE_STORAGE=False: files go to MEDIA_ROOT, nothing to do.")
            return
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
        try:
            service.create_container(settings.AZURE_STORAGE_CONTAINER)
            self.stdout.write(self.style.SUCCESS(f"Created container {settings.AZURE_STORAGE_CONTAINER!r}."))
        except ResourceExistsError:
            self.stdout.write(f"Container {settings.AZURE_STORAGE_CONTAINER!r} already exists.")
