from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path, re_path
from django.views.generic import RedirectView
from django.views.static import serve
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from scanner.views import download_apk, download_page


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("", RedirectView.as_view(pattern_name="scanner:index", permanent=False)),
    path("scan/", include("scanner.urls")),
    path("healthz/", healthz, name="healthz"),
    path("download/", download_page, name="download"),
    path("download/docscan.apk", download_apk, name="download-apk"),
    path("admin/", admin.site.urls),
    path("api/auth/", include("accounts.urls")),
    path("api/", include("documents.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]

if not settings.USE_AZURE_STORAGE and (settings.DEBUG or settings.SERVE_MEDIA):
    # `static()` only works with DEBUG=True, so register the view directly.
    media_prefix = settings.MEDIA_URL.strip("/")
    urlpatterns += [re_path(rf"^{media_prefix}/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT})]
