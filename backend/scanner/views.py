from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from django.shortcuts import render
from django.views.decorators.clickjacking import xframe_options_deny
from django.views.decorators.http import require_GET

# Android app build, copied here before building the Docker image (see mobile build steps in README).
APK_PATH = Path(settings.BASE_DIR) / "downloads" / "docscan.apk"


@require_GET
@xframe_options_deny
def scanner(request):
    """Single-page scanner UI. All data goes through the JWT-protected REST API."""
    return render(request, "scanner/index.html")


@require_GET
def download_page(request):
    """Landing page with a button to install the Android app."""
    available = APK_PATH.exists()
    size_mb = round(APK_PATH.stat().st_size / 1_048_576, 1) if available else None
    return render(request, "scanner/download.html", {"apk_available": available, "apk_size_mb": size_mb})


@require_GET
def download_apk(request):
    if not APK_PATH.exists():
        raise Http404("The Android app has not been uploaded yet.")
    response = FileResponse(
        APK_PATH.open("rb"),
        as_attachment=True,
        filename="DocScan.apk",
        content_type="application/vnd.android.package-archive",
    )
    response["Cache-Control"] = "no-cache"
    return response
