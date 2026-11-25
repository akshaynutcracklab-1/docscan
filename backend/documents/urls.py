"""Explicit routes for the APIViews.

Paths, regexes, URL names, the `/api/` root page and the `.json`-style format suffixes are
the same ones the DefaultRouter generated before, so clients and `reverse()` keep working.
"""
from django.urls import re_path
from rest_framework.routers import APIRootView
from rest_framework.urlpatterns import format_suffix_patterns

from . import views

app_name = "documents"

PK = r"(?P<pk>[^/.]+)"

urlpatterns = [
    re_path(r"^document-types/$", views.DocumentTypeListView.as_view(), name="document-type-list"),
    re_path(r"^document-types/(?P<code>[^/.]+)/$", views.DocumentTypeDetailView.as_view(), name="document-type-detail"),

    re_path(r"^documents/$", views.DocumentListCreateView.as_view(), name="document-list"),
    re_path(rf"^documents/{PK}/$", views.DocumentDetailView.as_view(), name="document-detail"),
    re_path(rf"^documents/{PK}/download/$", views.DocumentDownloadView.as_view(), name="document-download"),
    re_path(rf"^documents/{PK}/pages/reorder/$", views.DocumentPageReorderView.as_view(), name="document-reorder-pages"),
    re_path(rf"^documents/{PK}/reprocess/$", views.DocumentReprocessView.as_view(), name="document-reprocess"),
    re_path(rf"^documents/{PK}/retry/$", views.DocumentRetryView.as_view(), name="document-retry"),
    re_path(rf"^documents/{PK}/status/$", views.DocumentStatusView.as_view(), name="document-status"),
    re_path(rf"^documents/{PK}/submit/$", views.DocumentSubmitView.as_view(), name="document-submit"),
    re_path(rf"^documents/{PK}/pages/$", views.DocumentPageUploadView.as_view(), name="document-upload-page"),
    re_path(rf"^documents/{PK}/pages/bulk/$", views.DocumentPageBulkUploadView.as_view(),
            name="document-upload-pages-bulk"),

    re_path(rf"^pages/{PK}/$", views.PageDetailView.as_view(), name="page-detail"),

    # Browsable index at /api/ (what DefaultRouter provided).
    re_path(r"^$", APIRootView.as_view(api_root_dict={
        "document-types": "document-type-list",
        "documents": "document-list",
    }), name="api-root"),
]

urlpatterns = format_suffix_patterns(urlpatterns)
