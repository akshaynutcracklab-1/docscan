from django.contrib.staticfiles import finders


def test_root_redirects_to_scanner(client):
    resp = client.get("/")
    assert resp.status_code == 302 and resp["Location"] == "/scan/"


def test_scanner_page_renders_without_login(client):
    resp = client.get("/scan/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'id="video"' in html and 'id="shutter"' in html
    assert "/static/scanner/app.js" in html and "/static/scanner/app.css" in html
    assert "/static/scanner/docdetect.js" in html  # edge detection + perspective correction
    assert 'id="overlay"' in html and 'id="crop-canvas"' in html
    assert resp["X-Frame-Options"] == "DENY"


def test_scanner_static_files_exist():
    assert finders.find("scanner/app.js")
    assert finders.find("scanner/app.css")
    assert finders.find("scanner/docdetect.js")
