from django.conf import settings
from rest_framework import serializers

from processing.image_utils import InvalidImage, inspect_image

from .models import Document, DocumentType, Page


def file_url(request, field_file):
    """Temporary URL for a stored file: an Azure SAS URL (15 min) or a local media URL."""
    if not field_file or not field_file.name:
        return None
    url = field_file.url
    if request is not None and url.startswith("/"):
        url = request.build_absolute_uri(url)
    return url


def validate_upload(image):
    max_bytes = settings.MAX_PAGE_UPLOAD_BYTES
    if image.size > max_bytes:
        raise serializers.ValidationError(f"Each image must be {max_bytes // (1024 * 1024)} MB or smaller.")
    try:
        info = inspect_image(image)
    except InvalidImage as exc:
        raise serializers.ValidationError(str(exc)) from exc
    # Store with an extension that matches the real content.
    stem = (image.name or "page").rsplit(".", 1)[0]
    image.name = f"{stem}.{info.extension}"
    return image


class DocumentTypeSerializer(serializers.ModelSerializer):
    has_field_extraction = serializers.SerializerMethodField()

    class Meta:
        model = DocumentType
        fields = ("code", "name", "description", "icon", "max_pages", "display_fields", "has_field_extraction")

    def get_has_field_extraction(self, obj) -> bool:
        return bool(obj.azure_field_model)


class PageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    page_pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = Page
        fields = (
            "id", "order", "image_url", "page_pdf_url", "rotation", "width", "height", "file_size",
            "status", "ocr_text", "ocr_confidence", "is_low_quality",
        )
        read_only_fields = fields

    def get_image_url(self, obj) -> str | None:
        return file_url(self.context.get("request"), obj.image)

    def get_page_pdf_url(self, obj) -> str | None:
        return file_url(self.context.get("request"), obj.page_pdf)


class PageUploadSerializer(serializers.Serializer):
    image = serializers.FileField()
    order = serializers.IntegerField(min_value=1, required=False)
    rotation = serializers.ChoiceField(choices=Page.Rotation.choices, required=False, default=0)

    def validate_image(self, image):
        return validate_upload(image)


class BulkPageUploadSerializer(serializers.Serializer):
    images = serializers.ListField(child=serializers.FileField(), allow_empty=False)

    def validate_images(self, images):
        if len(images) > settings.MAX_BULK_UPLOAD_FILES:
            raise serializers.ValidationError(f"Upload at most {settings.MAX_BULK_UPLOAD_FILES} images at once.")
        errors, cleaned = {}, []
        for index, image in enumerate(images):
            try:
                cleaned.append(validate_upload(image))
            except serializers.ValidationError as exc:
                errors[index] = exc.detail
        if errors:
            raise serializers.ValidationError(errors)
        return cleaned


class PageReorderSerializer(serializers.Serializer):
    page_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)


class PageUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Page
        fields = ("rotation",)
        extra_kwargs = {"rotation": {"required": True}}


class DocumentListSerializer(serializers.ModelSerializer):
    document_type = serializers.SlugRelatedField(slug_field="code", read_only=True)
    document_type_name = serializers.CharField(source="document_type.name", read_only=True)
    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = (
            "id", "title", "document_type", "document_type_name", "status", "progress", "page_count",
            "average_confidence", "language", "thumbnail_url", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_thumbnail_url(self, obj) -> str | None:
        name = getattr(obj, "thumbnail_name", None)  # annotated by the list view
        return file_url(self.context.get("request"), Page(image=name).image) if name else None


class DocumentCreateSerializer(serializers.ModelSerializer):
    document_type = serializers.SlugRelatedField(slug_field="code", queryset=DocumentType.objects.filter(is_active=True))
    title = serializers.CharField(max_length=255, required=False, allow_blank=True)

    class Meta:
        model = Document
        fields = ("id", "title", "document_type", "status")
        read_only_fields = ("id", "status")


class DocumentUpdateSerializer(serializers.ModelSerializer):
    document_type = serializers.SlugRelatedField(
        slug_field="code", queryset=DocumentType.objects.filter(is_active=True), required=False
    )

    class Meta:
        model = Document
        fields = ("title", "document_type", "full_text")
        extra_kwargs = {"title": {"required": False}, "full_text": {"required": False}}

    def validate_title(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Title cannot be empty.")
        return value


class DocumentDetailSerializer(serializers.ModelSerializer):
    document_type = DocumentTypeSerializer(read_only=True)
    pages = PageSerializer(many=True, read_only=True)
    pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = (
            "id", "title", "document_type", "status", "progress", "current_step", "error_message",
            "page_count", "pages", "pdf_url", "full_text", "language", "extracted_fields", "analysis",
            "average_confidence", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_pdf_url(self, obj) -> str | None:
        return file_url(self.context.get("request"), obj.pdf_file)


class DocumentStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = ("id", "status", "progress", "current_step", "error_message", "page_count", "updated_at")
        read_only_fields = fields


class SubmitResponseSerializer(serializers.Serializer):
    document_id = serializers.UUIDField()
    status = serializers.CharField()
