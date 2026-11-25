import json

from django.conf import settings
from django.db.models import OuterRef, Prefetch, Subquery
from django.http import FileResponse, HttpResponse, HttpResponseRedirect
from django.utils.text import slugify
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from . import services
from .exceptions import Conflict
from .filters import DocumentFilter
from .models import Document, DocumentType, Page
from .permissions import IsDocumentOwner
from .selectors import documents_for
from .serializers import (
    BulkPageUploadSerializer,
    DocumentCreateSerializer,
    DocumentDetailSerializer,
    DocumentListSerializer,
    DocumentStatusSerializer,
    DocumentTypeSerializer,
    DocumentUpdateSerializer,
    PageReorderSerializer,
    PageSerializer,
    PageUpdateSerializer,
    PageUploadSerializer,
    SubmitResponseSerializer,
    file_url,
)
from .throttles import UploadRateThrottle

UPLOAD_THROTTLES = [UserRateThrottle, UploadRateThrottle]


def ordered_pages():
    return Prefetch("pages", queryset=Page.objects.order_by("order"))


class BaseAPIView(APIView):
    # Shared helpers. `get_queryset()` is also how drf-spectacular types the `{id}` path parameter.
    # (Comments, not docstrings: drf-spectacular would copy an inherited docstring into every operation.)

    def get_queryset(self):
        raise NotImplementedError

    def get_object_from(self, queryset, **lookup):
        """404 when missing (or not the user's), then run the object-level permission checks."""
        obj = get_object_or_404(queryset, **lookup)
        self.check_object_permissions(self.request, obj)
        return obj

    def get_serializer_context(self):
        return {"request": self.request, "format": self.format_kwarg, "view": self}


class DocumentAPIView(BaseAPIView):
    # Base for every view on one of the current user's documents.

    permission_classes = (IsAuthenticated, IsDocumentOwner)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation
            return Document.objects.none()
        return documents_for(self.request.user)

    def get_document(self, pk, queryset=None):
        return self.get_object_from(self.get_queryset() if queryset is None else queryset, pk=pk)

    def detail_data(self, document):
        document = self.get_queryset().prefetch_related(ordered_pages()).get(pk=document.pk)
        return DocumentDetailSerializer(document, context=self.get_serializer_context()).data


# ====================================================================== document types (step 1)

@extend_schema(tags=["document types"])
class DocumentTypeListView(BaseAPIView):
    """Step 1: the document types a user can choose from."""

    name = "Document Type List"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet
    def get_queryset(self):
        return DocumentType.objects.filter(is_active=True).order_by("sort_order", "name")

    @extend_schema(responses=DocumentTypeSerializer(many=True))
    def get(self, request, format=None):
        return Response(DocumentTypeSerializer(self.get_queryset(), many=True, context=self.get_serializer_context()).data)


@extend_schema(tags=["document types"])
class DocumentTypeDetailView(DocumentTypeListView):
    """Step 1: the document types a user can choose from."""

    name = "Document Type Instance"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet
    @extend_schema(responses=DocumentTypeSerializer)
    def get(self, request, code, format=None):
        document_type = self.get_object_from(self.get_queryset(), code=code)
        return Response(DocumentTypeSerializer(document_type, context=self.get_serializer_context()).data)


# ====================================================================== documents

@extend_schema(tags=["documents"])
class DocumentListCreateView(DocumentAPIView):
    name = "Document List"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet

    # Same classes the ViewSet got from REST_FRAMEWORK (PageNumberPagination, DjangoFilterBackend).
    pagination_class = api_settings.DEFAULT_PAGINATION_CLASS
    filter_backends = list(api_settings.DEFAULT_FILTER_BACKENDS)
    filterset_class = DocumentFilter

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request is None or self.request.method != "GET":
            return qs
        first_image = Page.objects.filter(document=OuterRef("pk")).order_by("order").values("image")[:1]
        return qs.annotate(thumbnail_name=Subquery(first_image))

    @extend_schema(summary="List my documents (filter by type/status, full-text search with q)",
                   responses=DocumentListSerializer(many=True))
    def get(self, request, format=None):
        queryset = self.get_queryset()
        for backend in self.filter_backends:
            queryset = backend().filter_queryset(request, queryset, self)
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = DocumentListSerializer(page, many=True, context=self.get_serializer_context()).data
        return paginator.get_paginated_response(data)

    @extend_schema(summary="Create a draft document", request=DocumentCreateSerializer,
                   responses={201: DocumentCreateSerializer})
    def post(self, request, format=None):
        serializer = DocumentCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        serializer.instance = services.create_document(
            owner=request.user,
            document_type=serializer.validated_data["document_type"],
            title=serializer.validated_data.get("title", ""),
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["documents"])
class DocumentDetailView(DocumentAPIView):
    name = "Document Instance"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet

    @extend_schema(summary="Document detail with pages, text, fields and analysis",
                   responses=DocumentDetailSerializer)
    def get(self, request, pk, format=None):
        document = self.get_document(pk, self.get_queryset().prefetch_related(ordered_pages()))
        return Response(DocumentDetailSerializer(document, context=self.get_serializer_context()).data)

    @extend_schema(summary="Rename, change type (before submit) or edit the text",
                   request=DocumentUpdateSerializer, responses={200: DocumentDetailSerializer})
    def patch(self, request, pk, format=None):
        document = self.get_document(pk)
        serializer = DocumentUpdateSerializer(document, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_document(document, serializer.validated_data)
        return Response(self.detail_data(document))

    @extend_schema(summary="Delete the document and all its files", responses={204: None})
    def delete(self, request, pk, format=None):
        self.get_document(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ====================================================================== pages (step 2)

@extend_schema(tags=["pages"])
class DocumentPageUploadView(DocumentAPIView):
    name = "Upload page"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet

    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = UPLOAD_THROTTLES

    @extend_schema(summary="Upload one page", request={"multipart/form-data": PageUploadSerializer},
                   responses={201: PageSerializer})
    def post(self, request, pk, format=None):
        document = self.get_document(pk)
        serializer = PageUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        [page] = services.add_pages(document, [data["image"]], order=data.get("order"), rotation=data["rotation"])
        return Response(PageSerializer(page, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["pages"])
class DocumentPageBulkUploadView(DocumentAPIView):
    name = "Upload pages bulk"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet

    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = UPLOAD_THROTTLES

    @extend_schema(summary="Upload several pages in order (field images[] or images)",
                   request={"multipart/form-data": BulkPageUploadSerializer}, responses={201: PageSerializer(many=True)})
    def post(self, request, pk, format=None):
        document = self.get_document(pk)
        files = request.FILES.getlist("images[]") or request.FILES.getlist("images")
        serializer = BulkPageUploadSerializer(data={"images": files})
        serializer.is_valid(raise_exception=True)
        pages = services.add_pages(document, serializer.validated_data["images"])
        data = PageSerializer(pages, many=True, context=self.get_serializer_context()).data
        return Response(data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["pages"])
class DocumentPageReorderView(DocumentAPIView):
    name = "Reorder pages"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet

    parser_classes = [JSONParser]

    @extend_schema(summary="Reorder pages", request=PageReorderSerializer, responses={200: PageSerializer(many=True)})
    def patch(self, request, pk, format=None):
        document = self.get_document(pk)
        serializer = PageReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.reorder_pages(document, serializer.validated_data["page_ids"])
        pages = document.pages.order_by("order")
        return Response(PageSerializer(pages, many=True, context=self.get_serializer_context()).data)


@extend_schema(tags=["pages"])
class PageDetailView(BaseAPIView):
    """Rotate (PATCH) or delete a single page. Remaining pages are renumbered on delete."""

    name = "Page Instance"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet
    permission_classes = (IsAuthenticated, IsDocumentOwner)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Page.objects.none()
        return Page.objects.filter(document__owner=self.request.user).select_related("document__document_type")

    @extend_schema(summary="Rotate a page", request=PageUpdateSerializer, responses={200: PageSerializer})
    def patch(self, request, pk, format=None):
        page = self.get_object_from(self.get_queryset(), pk=pk)
        serializer = PageUpdateSerializer(page, data=request.data, partial=True, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        services.update_page_rotation(page, serializer.validated_data["rotation"])
        return Response(PageSerializer(page, context=self.get_serializer_context()).data)

    @extend_schema(summary="Delete a page", responses={204: None})
    def delete(self, request, pk, format=None):
        services.delete_page(self.get_object_from(self.get_queryset(), pk=pk))
        return Response(status=status.HTTP_204_NO_CONTENT)


# ====================================================================== processing (step 3)

def started(document):
    return Response({"document_id": str(document.id), "status": document.status}, status=status.HTTP_202_ACCEPTED)


@extend_schema(tags=["processing"])
class DocumentSubmitView(DocumentAPIView):
    name = "Submit"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet

    @extend_schema(summary="Submit for processing", request=None,
                   responses={202: SubmitResponseSerializer, 409: OpenApiResponse(description="Already submitted")})
    def post(self, request, pk, format=None):
        return started(services.submit_document(self.get_document(pk)))


@extend_schema(tags=["processing"])
class DocumentRetryView(DocumentAPIView):
    name = "Retry"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet

    @extend_schema(summary="Retry a failed document", request=None, responses={202: SubmitResponseSerializer})
    def post(self, request, pk, format=None):
        return started(services.retry_document(self.get_document(pk)))


@extend_schema(tags=["processing"])
class DocumentReprocessView(DocumentAPIView):
    name = "Reprocess"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet

    @extend_schema(summary="Reprocess a ready document after changing its pages", request=None,
                   responses={202: SubmitResponseSerializer})
    def post(self, request, pk, format=None):
        return started(services.reprocess_document(self.get_document(pk)))


@extend_schema(tags=["processing"])
class DocumentStatusView(DocumentAPIView):
    name = "Status"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet

    @extend_schema(summary="Lightweight status for polling", responses=DocumentStatusSerializer)
    def get(self, request, pk, format=None):
        return Response(DocumentStatusSerializer(self.get_document(pk)).data)


# ====================================================================== downloads

@extend_schema(tags=["documents"])
class DocumentDownloadView(DocumentAPIView):
    name = "Download"  # DRF view name (OPTIONS / browsable API), as with the old ViewSet

    @extend_schema(
        summary="Download as pdf, txt or json",
        parameters=[
            OpenApiParameter("format", OpenApiTypes.STR, enum=["pdf", "txt", "json"], default="pdf"),
            OpenApiParameter("link", OpenApiTypes.BOOL, description="pdf only: return {url, expires_in} "
                             "instead of redirecting / streaming"),
        ],
        responses={
            (200, "application/pdf"): OpenApiTypes.BINARY,
            (200, "text/plain"): OpenApiTypes.STR,
            (200, "application/json"): inline_serializer(
                "DownloadLink", fields={"url": serializers.URLField(), "expires_in": serializers.IntegerField()}
            ),
            302: OpenApiResponse(description="Redirect to a signed Azure Blob URL (valid 15 minutes)"),
        },
    )
    def get(self, request, pk, format=None):
        document = self.get_document(pk)
        fmt = request.query_params.get("format", "pdf").lower()
        filename = slugify(document.title) or str(document.id)

        if fmt == "pdf":
            if not document.pdf_file:
                raise NotFound("The PDF is not available yet.")
            if request.query_params.get("link", "").lower() in ("1", "true", "yes"):
                return Response({
                    "url": file_url(request, document.pdf_file),
                    "expires_in": settings.SIGNED_URL_EXPIRY_SECONDS if settings.USE_AZURE_STORAGE else None,
                })
            if settings.USE_AZURE_STORAGE:
                return HttpResponseRedirect(document.pdf_file.url)
            return FileResponse(document.pdf_file.open("rb"), as_attachment=True, filename=f"{filename}.pdf",
                                content_type="application/pdf")

        if document.status == Document.Status.PROCESSING:
            raise Conflict("The document is still processing.")
        if fmt == "txt":
            response = HttpResponse(document.full_text, content_type="text/plain; charset=utf-8")
        elif fmt == "json":
            payload = {
                "id": str(document.id),
                "title": document.title,
                "document_type": document.document_type.code,
                "language": document.language,
                "page_count": document.page_count,
                "average_confidence": document.average_confidence,
                "extracted_fields": document.extracted_fields,
                "analysis": document.analysis,
            }
            response = HttpResponse(json.dumps(payload, ensure_ascii=False, indent=2),
                                    content_type="application/json; charset=utf-8")
        else:
            raise ValidationError({"format": ["Use pdf, txt or json."]})
        response["Content-Disposition"] = f'attachment; filename="{filename}.{fmt}"'
        return response
