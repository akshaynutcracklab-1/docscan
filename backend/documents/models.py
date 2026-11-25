import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def document_base_path(document) -> str:
    return f"users/{document.owner_id}/documents/{document.id}"


def page_image_upload_to(page, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"{document_base_path(page.document)}/pages/{page.order}.{ext}"


def page_pdf_upload_to(page, filename: str) -> str:
    return f"{document_base_path(page.document)}/pages/{page.order}.pdf"


def document_pdf_upload_to(document, filename: str) -> str:
    return f"{document_base_path(document)}/document.pdf"


class DocumentType(models.Model):
    """What the user picks in step 1. Editable from the admin, seeded by a fixture."""

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=50, blank=True, help_text="Icon name used by the frontend.")
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveIntegerField(default=0)
    azure_field_model = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Azure prebuilt model used for field extraction, e.g. prebuilt-invoice. Empty = OCR only.",
    )
    display_fields = models.JSONField(default=list, blank=True)
    max_pages = models.PositiveIntegerField(default=50, validators=[MinValueValidator(1)])

    class Meta:
        ordering = ("sort_order", "name")

    def __str__(self):
        return self.name


class Document(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        UPLOADING = "uploading", "Uploading"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    # Pages can be added / removed / reordered in these states. Ready and failed documents
    # are editable too; the changes take effect through `reprocess` / `retry`.
    PAGE_EDITABLE_STATUSES = (Status.DRAFT, Status.UPLOADING, Status.READY, Status.FAILED)
    SUBMITTABLE_STATUSES = (Status.DRAFT, Status.UPLOADING)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="documents")
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name="documents")
    title = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)

    page_count = models.PositiveIntegerField(default=0)
    pdf_file = models.FileField(upload_to=document_pdf_upload_to, max_length=500, blank=True)
    full_text = models.TextField(blank=True)
    language = models.CharField(max_length=20, blank=True)
    extracted_fields = models.JSONField(default=dict, blank=True)
    analysis = models.JSONField(default=dict, blank=True)
    average_confidence = models.FloatField(null=True, blank=True)

    error_message = models.TextField(blank=True)
    progress = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(100)])
    current_step = models.CharField(max_length=50, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    search_vector = SearchVectorField(null=True, editable=False)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("owner", "-created_at"), name="doc_owner_created_idx"),
            models.Index(fields=("owner", "status"), name="doc_owner_status_idx"),
            # Created only on PostgreSQL (see migration 0002); SQLite has no GIN.
            GinIndex(fields=("search_vector",), name="doc_search_vector_gin"),
        ]

    def __str__(self):
        return self.title

    @property
    def base_path(self) -> str:
        return document_base_path(self)

    @property
    def is_page_editable(self) -> bool:
        return self.status in self.PAGE_EDITABLE_STATUSES


class Page(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    class Rotation(models.IntegerChoices):
        R0 = 0, "0°"
        R90 = 90, "90°"
        R180 = 180, "180°"
        R270 = 270, "270°"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="pages")
    order = models.PositiveIntegerField()
    image = models.ImageField(upload_to=page_image_upload_to, max_length=500, width_field="width", height_field="height")
    rotation = models.PositiveSmallIntegerField(
        choices=Rotation.choices, default=0, help_text="Clockwise rotation still to apply to the image."
    )
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    file_size = models.PositiveIntegerField(default=0)

    ocr_text = models.TextField(blank=True)
    ocr_confidence = models.FloatField(null=True, blank=True)
    ocr_data = models.JSONField(default=dict, blank=True)
    page_pdf = models.FileField(upload_to=page_pdf_upload_to, max_length=500, blank=True)
    is_low_quality = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("document", "order")
        constraints = [models.UniqueConstraint(fields=("document", "order"), name="page_unique_order_per_document")]

    def __str__(self):
        return f"{self.document_id} p{self.order}"
