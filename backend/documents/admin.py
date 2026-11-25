from django.contrib import admin

from .models import Document, DocumentType, Page


@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "azure_field_model", "max_pages", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active", "max_pages")
    list_filter = ("is_active",)
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}


class PageInline(admin.TabularInline):
    model = Page
    extra = 0
    fields = ("order", "image", "rotation", "status", "ocr_confidence", "is_low_quality", "width", "height", "file_size")
    readonly_fields = ("ocr_confidence", "is_low_quality", "width", "height", "file_size")
    ordering = ("order",)


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "document_type", "status", "progress", "page_count", "average_confidence", "created_at")
    list_filter = ("status", "document_type")
    search_fields = ("title", "owner__username", "owner__email", "id")
    raw_id_fields = ("owner",)
    readonly_fields = ("id", "created_at", "updated_at", "progress", "current_step", "average_confidence")
    date_hierarchy = "created_at"
    inlines = (PageInline,)
    list_select_related = ("owner", "document_type")


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ("document", "order", "status", "ocr_confidence", "is_low_quality", "file_size")
    list_filter = ("status", "is_low_quality")
    raw_id_fields = ("document",)
    search_fields = ("document__title", "document__id")
