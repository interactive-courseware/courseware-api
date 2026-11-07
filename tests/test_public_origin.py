import pytest
from conftest import login
from fastapi.testclient import TestClient
from pydantic import ValidationError

from courseware.config import Settings, settings
from courseware.main import app


def test_public_http_requires_explicit_opt_in():
    config = dict(public_origin="http://courses.example.org:18814", secure_cookies=False)
    with pytest.raises(ValidationError, match="CW_ALLOW_INSECURE_HTTP"):
        Settings(**config, allow_insecure_http=False)
    assert Settings(**config, allow_insecure_http=True).public_origin == config["public_origin"]
    with pytest.raises(ValidationError, match="HTTPS requires Secure cookies"):
        Settings(public_origin="https://courses.example.org", secure_cookies=False, allow_insecure_http=True)


def test_forwarded_http_login_preserves_origin_and_csrf_checks(monkeypatch, user_factory):
    monkeypatch.setenv("CW_PUBLIC_ORIGIN", "http://courses.example.org:18814")
    monkeypatch.setenv("CW_SECURE_COOKIES", "false")
    monkeypatch.setenv("CW_ALLOW_INSECURE_HTTP", "true")
    settings.cache_clear()
    user_factory()
    with TestClient(app, base_url=settings().public_origin) as client:
        response = login(client)
        cookie = response.headers["set-cookie"]
        assert "Secure" not in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert client.get("/api/v1/auth/me").status_code == 200
        assert client.post("/api/v1/auth/logout", headers={"Origin": "http://untrusted.example"}).status_code == 403
        assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "invalid"}).status_code == 403
        assert client.post("/api/v1/auth/logout").status_code == 204
