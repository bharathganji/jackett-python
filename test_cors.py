import os
import pytest
from fastapi.testclient import TestClient

# We need to set the environment variable before importing the app
os.environ["ALLOWED_ORIGINS"] = "http://localhost:3000,https://my-trusted-domain.com"
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
