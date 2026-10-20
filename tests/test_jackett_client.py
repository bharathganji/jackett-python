import pytest
import os
from unittest.mock import patch, MagicMock
from app.services.jackett_client import get_jackett_cookie, fetch_indexers_data

def test_http_client_verify_true():
    with patch('app.services.jackett_client.httpx.Client') as mock_client:
        mock_instance = MagicMock()
        mock_instance.get.return_value = MagicMock(status_code=200)
        mock_instance.cookies.get.return_value = "dummy_cookie"

        # Make the context manager return our mock_instance
        mock_client.return_value.__enter__.return_value = mock_instance

        get_jackett_cookie()

        # Verify that httpx.Client was called with verify=True
        mock_client.assert_called_with(follow_redirects=True, verify=True)

@pytest.mark.asyncio
async def test_async_http_client_verify_true():
    with patch('app.services.jackett_client.httpx.AsyncClient') as mock_client:
        mock_instance = MagicMock()
        mock_response = MagicMock(status_code=200, is_redirect=False)
        mock_response.json.return_value = [{"id": "1", "configured": True}]

        # Create an async mock for get
        async def mock_get(*args, **kwargs):
            return mock_response

        mock_instance.get = mock_get

        # Make the async context manager return our mock_instance
        mock_client.return_value.__aenter__.return_value = mock_instance

        await fetch_indexers_data("dummy_cookie")

        # Verify that httpx.AsyncClient was called with verify=True
        mock_client.assert_called_with(timeout=30.0, follow_redirects=True, verify=True)
