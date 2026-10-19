"""The PWA files (TASK-065, PRD section 8 "Installable"): the manifest, the home-screen icons and
the minimal service worker, all served from the root path and without a sign in."""

import json
import struct

import pytest

from tests.conftest import TEST_SETTINGS


@pytest.fixture
def app_client():
    from fastapi.testclient import TestClient

    from app.core.config import load_settings
    from app.main import create_app

    return TestClient(create_app(load_settings(TEST_SETTINGS)))


def png_size(content):
    # a PNG keeps its width and height at bytes 16 to 24
    return struct.unpack(">II", content[16:24])


def test_the_manifest_is_served_from_the_root_without_a_sign_in(app_client):
    response = app_client.get("/manifest.json", follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(response.text)
    assert manifest["name"] == "C&C H2O Water Billing"
    assert (manifest["start_url"], manifest["scope"], manifest["display"]) == ("/", "/", "standalone")


def test_the_manifest_icons_exist_at_the_sizes_it_names(app_client):
    manifest = app_client.get("/manifest.json").json()

    purposes = []
    for icon in manifest["icons"]:
        response = app_client.get(icon["src"])
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        width, height = png_size(response.content)
        assert f"{width}x{height}" == icon["sizes"]
        purposes.append((icon["sizes"], icon["purpose"]))
    # 192 and 512 for the home screen, and a maskable one (TASK-065)
    assert purposes == [("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")]


def test_the_service_worker_is_served_from_the_root_without_a_sign_in(app_client):
    response = app_client.get("/sw.js", follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    # the browser checks for a new version every time
    assert response.headers["cache-control"] == "no-cache"
    # the security headers are on these files too
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_service_worker_keeps_only_shell_files_that_exist(app_client):
    script = app_client.get("/sw.js").text

    shell_block = script.split("var SHELL_FILES = [")[1].split("];")[0]
    shell_files = [line.strip().strip(",").strip('"') for line in shell_block.splitlines() if line.strip()]
    assert len(shell_files) == 12
    for path in shell_files:
        assert path.startswith("/static/shared/")
        assert app_client.get(path).status_code == 200


def test_the_service_worker_never_stores_a_page(app_client):
    script = app_client.get("/sw.js").text

    # only the listed shell files are answered by the worker; pages and forms go to the server
    assert "SHELL_FILES.indexOf(url.pathname) !== -1" in script
    assert 'event.request.method === "GET"' in script
    assert "cache.put" not in script


def test_every_page_links_the_manifest_and_registers_the_worker(app_client):
    page = app_client.get("/login").text
    script = app_client.get("/static/shared/js/app.js").text

    assert '<link rel="manifest" href="/manifest.json">' in page
    assert '<link rel="apple-touch-icon" href="/static/shared/img/icon-192.png">' in page
    assert 'navigator.serviceWorker.register("/sw.js")' in script
