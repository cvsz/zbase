"""The management console must be served and must obey the CSP.

A console that 404s, or that needs an inline script, is a broken console; the
strict Content-Security-Policy makes an inline script silently fail, so that
case is checked explicitly rather than discovered in a browser.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.integration


def test_console_is_served_at_the_root(auth_client):
    response = auth_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "S3Vault" in response.text


def test_console_assets_are_served(auth_client):
    for path in ("/assets/app.js", "/assets/styles.css"):
        response = auth_client.get(path)
        assert response.status_code == 200, path
        assert response.content


def test_console_uses_no_inline_script(auth_client):
    """CSP is `script-src 'self'`; an inline script would be blocked silently."""
    html = auth_client.get("/").text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script> found"
    assert not re.search(r"<style[^>]*>", html), "inline <style> found"
    assert not re.search(r"\son\w+\s*=", html), "inline event handler attribute found"


def test_console_loads_the_csrf_cookie(auth_client):
    """The console needs a readable cookie to echo the CSRF header."""
    html = auth_client.get("/").text
    assert "/assets/app.js" in html
    assert auth_client.cookies.get("s3vault_csrf")


def test_csp_header_is_present_and_strict(auth_client):
    csp = auth_client.get("/").headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp


def test_spa_fallback_does_not_shadow_the_api(auth_client):
    """An unknown non-API path serves the SPA; an unknown API path 404s as JSON."""
    spa = auth_client.get("/buckets/does-not-exist/page")
    assert spa.status_code == 200
    assert "text/html" in spa.headers["content-type"]

    api = auth_client.get("/api/v1/does-not-exist")
    assert api.status_code == 404
    assert api.headers["content-type"].startswith("application/json")


def test_openapi_is_available_outside_production(auth_client):
    """Test profile serves the schema; production disables it."""
    response = auth_client.get("/api/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "paths" in schema
    assert "/api/v1/buckets" in schema["paths"]


def test_console_shows_no_fabricated_data(auth_client):
    """No placeholder buckets, objects or metrics are baked into the console."""
    for path in ("/", "/assets/app.js"):
        text = auth_client.get(path).text.lower()
        for fabrication in ("sample bucket", "demo bucket", "lorem ipsum",
                            "example.com/bucket", "fake-object"):
            assert fabrication not in text, f"{path} contains fabricated content: {fabrication}"