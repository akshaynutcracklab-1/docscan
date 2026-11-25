import io

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from documents.models import DocumentType


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()  # throttle counters and the rate limiter live in the cache
    yield
    cache.clear()


@pytest.fixture
def document_types(db):
    call_command("loaddata", "document_types", verbosity=0)
    return {t.code: t for t in DocumentType.objects.all()}


def make_user(username="alice"):
    return User.objects.create_user(username=username, email=f"{username}@example.com", password="S3cure-pass!")


@pytest.fixture
def user(db):
    return make_user("alice")


@pytest.fixture
def other_user(db):
    return make_user("bob")


def client_for(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
    return client


@pytest.fixture
def api(user):
    return client_for(user)


@pytest.fixture
def other_api(other_user):
    return client_for(other_user)


def image_bytes(fmt="JPEG", size=(400, 600), color=(240, 240, 240), **save_kwargs) -> bytes:
    buf = io.BytesIO()
    mode = "RGBA" if fmt == "PNG" else "RGB"
    Image.new(mode, size, color).save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


def upload(name="page.jpg", fmt="JPEG", content_type="image/jpeg", **kwargs) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, image_bytes(fmt, **kwargs), content_type=content_type)


@pytest.fixture
def make_document(api, document_types):
    def _make(code="invoice", pages=0, title=None):
        payload = {"document_type": code}
        if title:
            payload["title"] = title
        resp = api.post("/api/documents/", payload, format="json")
        assert resp.status_code == 201, resp.content
        doc_id = resp.json()["id"]
        for i in range(pages):
            r = api.post(f"/api/documents/{doc_id}/pages/", {"image": upload(f"p{i}.jpg")}, format="multipart")
            assert r.status_code == 201, r.content
        return doc_id

    return _make
