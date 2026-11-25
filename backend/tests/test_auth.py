import pytest

pytestmark = pytest.mark.django_db


def test_register_returns_tokens_and_user(client):
    resp = client.post(
        "/api/auth/register/",
        {"username": "carol", "email": "Carol@Example.com", "password": "Very-secure-123"},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["user"]["username"] == "carol"
    assert body["user"]["email"] == "carol@example.com"
    assert body["access"] and body["refresh"]
    assert "password" not in body["user"]


def test_register_rejects_weak_password_and_duplicate_email(client, user):
    weak = client.post("/api/auth/register/", {"username": "dan", "email": "d@example.com", "password": "123"},
                       content_type="application/json")
    assert weak.status_code == 400
    assert "password" in str(weak.json()).lower() or "non_field_errors" in weak.json()

    dup = client.post("/api/auth/register/", {"username": "dan", "email": user.email.upper(), "password": "Very-secure-123"},
                      content_type="application/json")
    assert dup.status_code == 400
    assert "email" in dup.json()


def test_login_refresh_and_me(client, user):
    token = client.post("/api/auth/token/", {"username": "alice", "password": "S3cure-pass!"},
                        content_type="application/json")
    assert token.status_code == 200
    access, refresh = token.json()["access"], token.json()["refresh"]

    me = client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {access}")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"

    refreshed = client.post("/api/auth/token/refresh/", {"refresh": refresh}, content_type="application/json")
    assert refreshed.status_code == 200
    assert refreshed.json()["access"]


def test_login_wrong_password_and_anonymous_access(client, user):
    bad = client.post("/api/auth/token/", {"username": "alice", "password": "nope"}, content_type="application/json")
    assert bad.status_code == 401
    assert client.get("/api/auth/me/").status_code == 401
    assert client.get("/api/documents/").status_code == 401
