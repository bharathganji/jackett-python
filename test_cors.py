import os
import pytest
from fastapi.testclient import TestClient

# We need to set the environment variable before importing the app
os.environ["ALLOWED_ORIGINS"] = "http://localhost:3000,https://my-trusted-domain.com,https://*.pikpak-plus.com"
os.environ["JACKETT_API_URL"] = "http://localhost:9117"
os.environ["API_KEY"] = "fake_key"

from app.api import app
from app.services.jackett_client import settings

client = TestClient(app)

def test_cors_allowed_origin():
    response = client.options(
        "/",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"

def test_cors_disallowed_origin():
    response = client.options(
        "/",
        headers={
            "Origin": "http://malicious-site.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400 or response.headers.get("access-control-allow-origin") is None

def test_cors_wildcard_origin():
    # Valid wildcard subdomain match
    response = client.options(
        "/",
        headers={
            "Origin": "https://jackett.pikpak-plus.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://jackett.pikpak-plus.com"

    # Invalid matching wildcard (http instead of https)
    response_invalid = client.options(
        "/",
        headers={
            "Origin": "http://jackett.pikpak-plus.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response_invalid.status_code == 400 or response_invalid.headers.get("access-control-allow-origin") is None
