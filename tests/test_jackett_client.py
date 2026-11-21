import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.services.cache import (
    clear_jackett_cookie_cache,
    get_jackett_cookie_from_cache,
    set_jackett_cookie_in_cache,
)
from app.services.jackett_client import (
    _fetch_results_from_jackett,
    fetch_indexers_data,
    get_jackett_cookie,
    get_jackett_cookie_cached,
)


@pytest.mark.asyncio
async def test_http_client_verify_true():
    with patch('app.services.jackett_client.httpx.AsyncClient') as mock_client:
        mock_instance = MagicMock()
        mock_response = MagicMock(status_code=200, is_redirect=False)
        mock_instance.cookies.get.return_value = "dummy_cookie"

        async def mock_get(*args, **kwargs):
            return mock_response

        mock_instance.get = mock_get
        mock_client.return_value.__aenter__.return_value = mock_instance

        await get_jackett_cookie()

        mock_client.assert_called_with(follow_redirects=True, verify=True)


@pytest.mark.asyncio
async def test_async_http_client_verify_true():
    with patch('app.services.jackett_client.httpx.AsyncClient') as mock_client:
        mock_instance = MagicMock()
        mock_response = MagicMock(status_code=200, is_redirect=False, is_error=False, history=[])
        mock_response.url = httpx.URL("https://search.pikpak-plus.com/api/v2.0/indexers")
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = [{"id": "1", "configured": True}]

        async def mock_get(*args, **kwargs):
            return mock_response

        mock_instance.get = mock_get
        mock_client.return_value.__aenter__.return_value = mock_instance

        await fetch_indexers_data("dummy_cookie")

        mock_client.assert_called_with(timeout=30.0, follow_redirects=True, verify=True)


@pytest.mark.asyncio
async def test_fetch_indexers_data_expired_cookie_reauth_and_retry():
    """
    Test that when an expired/invalid cookie causes Jackett to redirect to /UI/Dashboard (HTML),
    fetch_indexers_data detects auth failure, invalidates cookie cache, gets a new cookie,
    and retries successfully.
    """
    # 1. Stale response: followed redirect to /UI/Dashboard with HTML
    history_resp = MagicMock(status_code=302)
    history_resp.url = httpx.URL("https://search.pikpak-plus.com/UI/Login?ReturnUrl=%2Fapi%2Fv2.0%2Findexers")
    history_resp.headers = {"location": "https://search.pikpak-plus.com/UI/Login"}

    stale_response = MagicMock(status_code=200, is_redirect=False, is_error=False)
    stale_response.url = httpx.URL("https://search.pikpak-plus.com/UI/Dashboard")
    stale_response.headers = {"content-type": "text/html"}
    stale_response.history = [history_resp]
    stale_response.text = "<!DOCTYPE html><html>Dashboard</html>"
    stale_response.json.side_effect = json.JSONDecodeError("Expecting value", "<!DOCTYPE html>", 0)

    # 2. Fresh response on retry: 200 OK JSON
    fresh_response = MagicMock(status_code=200, is_redirect=False, is_error=False)
    fresh_response.url = httpx.URL("https://search.pikpak-plus.com/api/v2.0/indexers")
    fresh_response.headers = {"content-type": "application/json"}
    fresh_response.history = []
    fresh_response.json.return_value = [{"id": "0magnet", "configured": True}]

    call_count = 0

    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return stale_response
        return fresh_response

    mock_instance = MagicMock()
    mock_instance.get = mock_get

    with patch('app.services.jackett_client.httpx.AsyncClient') as mock_client, \
         patch('app.services.jackett_client.get_jackett_cookie_cached', new_callable=AsyncMock) as mock_get_cookie:
        mock_client.return_value.__aenter__.return_value = mock_instance
        mock_get_cookie.return_value = "new_fresh_cookie"

        result = await fetch_indexers_data("stale_cookie")

        assert result == [{"id": "0magnet", "configured": True}]
        assert call_count == 2
        mock_get_cookie.assert_called_with(force_refresh=True)


@pytest.mark.asyncio
async def test_fetch_indexers_data_html_decode_error_after_retry():
    """
    Test that if the response still cannot be parsed as JSON even after retry,
    an HTTPException(500) with INVALID_RESPONSE_FORMAT is raised.
    """
    bad_response = MagicMock(status_code=200, is_redirect=False, is_error=False, history=[])
    bad_response.url = httpx.URL("https://search.pikpak-plus.com/api/v2.0/indexers")
    bad_response.headers = {"content-type": "text/plain"}
    bad_response.json.side_effect = json.JSONDecodeError("Expecting value", "bad_response", 0)

    mock_instance = MagicMock()
    mock_instance.get = AsyncMock(return_value=bad_response)

    with patch('app.services.jackett_client.httpx.AsyncClient') as mock_client:
        mock_client.return_value.__aenter__.return_value = mock_instance

        with pytest.raises(HTTPException) as exc_info:
            await fetch_indexers_data("dummy_cookie")

        assert exc_info.value.status_code == 500
        assert "Invalid response format" in exc_info.value.detail


@pytest.mark.asyncio
async def test_clear_jackett_cookie_cache():
    """
    Test that set_jackett_cookie_in_cache and clear_jackett_cookie_cache operate correctly.
    """
    set_jackett_cookie_in_cache("sample_cookie_val")
    assert get_jackett_cookie_from_cache() == "sample_cookie_val"

    clear_jackett_cookie_cache()
    assert get_jackett_cookie_from_cache() is None


@pytest.mark.asyncio
async def test_get_jackett_cookie_cached_force_refresh():
    """
    Test that get_jackett_cookie_cached with force_refresh=True calls get_jackett_cookie
    even when a cookie is already in cache.
    """
    set_jackett_cookie_in_cache("existing_cookie")

    with patch('app.services.jackett_client.get_jackett_cookie', new_callable=AsyncMock) as mock_login:
        mock_login.return_value = "refreshed_cookie"

        # Without force_refresh, uses cached cookie
        cached = await get_jackett_cookie_cached(force_refresh=False)
        assert cached == "existing_cookie"
        mock_login.assert_not_called()

        # With force_refresh, fetches new cookie
        refreshed = await get_jackett_cookie_cached(force_refresh=True)
        assert refreshed == "refreshed_cookie"
        mock_login.assert_called_once()
        assert get_jackett_cookie_from_cache() == "refreshed_cookie"


@pytest.mark.asyncio
async def test_fetch_results_from_jackett_expired_cookie_retry():
    """
    Test that _fetch_results_from_jackett retries when redirected to login.
    """
    auth_redirect = MagicMock(status_code=302, is_redirect=True, history=[])
    auth_redirect.url = httpx.URL("https://search.pikpak-plus.com/UI/Login")
    auth_redirect.headers = {"location": "https://search.pikpak-plus.com/UI/Login"}

    success_resp = MagicMock(status_code=200, is_redirect=False, is_error=False, history=[])
    success_resp.url = httpx.URL("https://search.pikpak-plus.com/api/v2.0/indexers/0magnet/results")
    success_resp.headers = {"content-type": "application/json"}
    success_resp.json.return_value = {"Results": [{"Title": "Test Torrent"}]}

    call_count = 0

    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return auth_redirect
        return success_resp

    mock_instance = MagicMock()
    mock_instance.get = mock_get

    with patch('app.services.jackett_client.httpx.AsyncClient') as mock_client, \
         patch('app.services.jackett_client.get_jackett_cookie_cached', new_callable=AsyncMock) as mock_get_cookie:
        mock_client.return_value.__aenter__.return_value = mock_instance
        mock_get_cookie.return_value = "brand_new_cookie"

        results = await _fetch_results_from_jackett("0magnet", "test query", "old_cookie")

        assert len(results) == 1
        assert results[0]["Title"] == "Test Torrent"
        assert call_count == 2

