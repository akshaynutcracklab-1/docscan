from django.urls import path

from .views import scanner

app_name = "scanner"

urlpatterns = [
    path("", scanner, name="index"),
]
