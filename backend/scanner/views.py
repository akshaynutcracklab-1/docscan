from django.views.decorators.clickjacking import xframe_options_deny
from django.views.decorators.http import require_GET
from django.shortcuts import render


@require_GET
@xframe_options_deny
def scanner(request):
    """Single-page scanner UI. All data goes through the JWT-protected REST API."""
    return render(request, "scanner/index.html")
