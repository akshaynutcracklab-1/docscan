import pytest

from scanner import views


@pytest.fixture
def apk(tmp_path, monkeypatch):
    path = tmp_path / "docscan.apk"
    monkeypatch.setattr(views, "APK_PATH", path)
    return path


def test_download_page_without_apk(client, apk):
    resp = client.get("/download/")
    assert resp.status_code == 200
    assert "Not available yet" in resp.content.decode()
    assert client.get("/download/docscan.apk").status_code == 404


def test_download_page_and_apk(client, apk):
    apk.write_bytes(b"PK" + b"\0" * 2048)
    html = client.get("/download/").content.decode()
    assert 'href="/download/docscan.apk"' in html

    resp = client.get("/download/docscan.apk")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/vnd.android.package-archive"
    assert 'attachment; filename="DocScan.apk"' in resp["Content-Disposition"]
    assert b"".join(resp.streaming_content) == apk.read_bytes()
