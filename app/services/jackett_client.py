import asyncio
import ipaddress
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.cache import (
    clear_jackett_cookie_cache,
    get_jackett_cookie_from_cache,
    set_jackett_cookie_in_cache,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants for error messages
INVALID_RESPONSE_FORMAT = "Invalid response format from Jackett API"
NO_CONFIGURED_INDEXERS = "No configured indexers found"

_cookie_lock = asyncio.Lock()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    JACKETT_API_URL: str = ""
    API_KEY: str = ""
    PORT: int = 8000


settings = Settings()
JACKETT_API_URL = settings.JACKETT_API_URL
API_KEY = settings.API_KEY


def is_secure_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == 'https':
        return True

    hostname = parsed.hostname
    if not hostname:
        return False

    if hostname in ('localhost', '127.0.0.1', '::1'):
        return True

    try:
        # Allow private IP networks (RFC 1918)
        ip = ipaddress.ip_address(hostname)
        if ip.is_private:
            return True
    except ValueError:
        pass

    # Allow dotless hostnames (e.g. docker service names like 'jackett')
    if '.' not in hostname:
        return True

    return False


def _is_auth_failure_response(response: httpx.Response) -> bool:
    """
    Check if response indicates an authentication failure (redirect to login, 401/403, or HTML page).
    """
    # Explicit auth status codes
    if response.status_code in (401, 403):
        return True

    # Redirect to Login
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "")
        if "/UI/Login" in location or "login" in location.lower():
            return True

    # Check redirect history if follow_redirects=True followed redirects to login/dashboard
    if response.history:
        for history_resp in response.history:
            loc = history_resp.headers.get("Location", "")
            if "/UI/Login" in loc or "login" in loc.lower():
                return True

    # If final URL is login or dashboard
    url_str = str(response.url)
    if any(path in url_str for path in ("/UI/Login", "/UI/Dashboard")):
        return True

    # If content-type is HTML and URL is not the API endpoint
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        return True

    return False


async def fetch_indexers_data(jackett_cookie: str | None = None) -> list[dict[str, Any]]:
    """
    Fetch raw indexer data from Jackett API.
    Common function used by get_configured_indexers and get_detailed_configured_indexers.
    If the provided or cached cookie is expired/invalid, automatically refreshes the cookie and retries once.
    """
    if not is_secure_url(JACKETT_API_URL):
        raise HTTPException(status_code=400, detail="Insecure JACKETT_API_URL: HTTPS or localhost is required to transmit credentials securely")

    cookie = jackett_cookie.strip() if jackett_cookie else ""
    if not cookie:
        cookie = await get_jackett_cookie_cached()
        if not cookie:
            raise HTTPException(status_code=400, detail="Jackett cookie is required")

    params = {"apikey": API_KEY, "configured": 'true'}
    headers = {
        'Cookie': f'Jackett={cookie.strip()}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    url = f"{JACKETT_API_URL}/api/v2.0/indexers"

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=True) as client:
            response = await client.get(url, params=params, headers=headers)

            # Check if auth failure occurred (e.g. redirected to login/dashboard or HTML response)
            if _is_auth_failure_response(response):
                logger.warning(
                    f"Jackett cookie invalid or expired (status={response.status_code}, url={response.url}). "
                    "Clearing cookie cache, refreshing, and retrying..."
                )
                clear_jackett_cookie_cache()
                new_cookie = await get_jackett_cookie_cached(force_refresh=True)
                if not new_cookie:
                    raise HTTPException(status_code=401, detail="Failed to re-authenticate with Jackett")

                headers['Cookie'] = f'Jackett={new_cookie.strip()}'
                response = await client.get(url, params=params, headers=headers)

                if _is_auth_failure_response(response):
                    logger.error(f"Authentication failed even after refreshing cookie: {response.status_code}")
                    raise HTTPException(
                        status_code=401,
                        detail="Authentication failed with Jackett API after cookie refresh"
                    )

            # Check for redirect responses that weren't auth-related (manual follow if needed)
            if response.is_redirect:
                logger.warning(f"Redirect detected when fetching indexers: {response.status_code}")
                # Follow the redirect manually
                redirect_url = response.headers.get('Location')
                if redirect_url:
                    logger.info(f"Following redirect to: {redirect_url}")
                    response = await client.get(redirect_url, params=params, headers=headers)

            # Ensure we have a successful response
            response.raise_for_status()

            try:
                indexers_data = response.json()
            except (ValueError, json.JSONDecodeError) as e:
                logger.error(f"Failed to parse indexers JSON response: {e}")
                raise HTTPException(status_code=500, detail=INVALID_RESPONSE_FORMAT) from e

            if not isinstance(indexers_data, list):
                raise HTTPException(status_code=500, detail=INVALID_RESPONSE_FORMAT)

            return indexers_data

    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        error_message = f"Jackett API HTTP error: {e.response.status_code}"
        logger.error(error_message)
        raise HTTPException(status_code=e.response.status_code, detail=error_message) from e
    except httpx.TimeoutException as e:
        error_message = "Timeout connecting to Jackett API"
        logger.error(error_message)
        raise HTTPException(status_code=504, detail=error_message) from e
    except httpx.HTTPError as e:
        error_message = f"Error connecting to Jackett API: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e
    except Exception as e:
        error_message = f"Unexpected error fetching indexers: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e


async def get_configured_indexers(jackett_cookie: str | None = None) -> list[str]:
    indexers_data = await fetch_indexers_data(jackett_cookie)
    configured_indexers = [
        indexer["id"] for indexer in indexers_data
        if isinstance(indexer, dict) and indexer.get("configured", False) and indexer.get("id")
    ]
    if not configured_indexers:
        logger.warning(NO_CONFIGURED_INDEXERS)
    return configured_indexers


async def get_detailed_configured_indexers(jackett_cookie: str | None = None) -> list[dict[str, str]]:
    """
    Get detailed information about configured indexers including id and site_link.
    Returns a list of dictionaries with 'id' and 'site_link' fields.
    """
    indexers_data = await fetch_indexers_data(jackett_cookie)
    detailed_indexers = []
    for indexer in indexers_data:
        if (isinstance(indexer, dict) and
            indexer.get("configured", False) and
            indexer.get("id")):
            detailed_indexers.append({
                "id": indexer["id"],
                "site_link": indexer.get("site_link", "")
            })
    if not detailed_indexers:
        logger.warning(NO_CONFIGURED_INDEXERS)
    return detailed_indexers


async def _fetch_results_from_jackett(indexer_id: str, query: str, jackett_cookie: str) -> list[dict[str, Any]]:
    """
    Helper function to fetch results from the Jackett API.
    Raises ValueError or httpx exceptions on failure.
    Returns a list of results on success.
    """
    if not indexer_id or not indexer_id.strip():
        raise ValueError("Invalid indexer_id provided")

    if not query or not query.strip():
        raise ValueError("Invalid query provided")

    if not jackett_cookie or not jackett_cookie.strip():
        raise ValueError("Invalid jackett_cookie provided")

    url = f"{JACKETT_API_URL}/api/v2.0/indexers/{indexer_id.strip()}/results"
    params = {"apikey": API_KEY, "Query": query.strip()}
    headers = {
        'Cookie': f'Jackett={jackett_cookie.strip()}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9'
    }

    async with httpx.AsyncClient(timeout=60, follow_redirects=True, verify=True) as client:
        response = await client.get(url, params=params, headers=headers)

        # Handle auth expiration / invalid session
        if _is_auth_failure_response(response):
            logger.warning(f"Auth failure fetching results for {indexer_id}, refreshing cookie and retrying...")
            clear_jackett_cookie_cache()
            new_cookie = await get_jackett_cookie_cached(force_refresh=True)
            if new_cookie:
                headers['Cookie'] = f'Jackett={new_cookie.strip()}'
                response = await client.get(url, params=params, headers=headers)

        response.raise_for_status()

        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as e:
            raise ValueError(f"Invalid response format from Jackett API (expected JSON): {e}") from e

        if not isinstance(data, dict):
            raise ValueError("Invalid response format from Jackett API")

        results = data.get("Results", [])
        if not isinstance(results, list):
            raise ValueError("Invalid results format from Jackett API")

        return results


async def _enqueue_message(result_queue, indexer_id: str, message_type: str, data=None):
    """
    Helper function to enqueue a message for stream_jackett_results_for_indexer.
    """
    try:
        await result_queue.put({
            "type": message_type,
            "indexer_id": indexer_id,
            "data": data
        })
    except Exception as queue_error:
        logger.error(f"Failed to put {message_type} in queue for {indexer_id}: {str(queue_error)}")


async def fetch_jackett_results_for_indexer(indexer_id: str, query: str, jackett_cookie: str) -> list[dict[str, Any]]:
    """
    Fetch results from a single indexer.
    Returns a list of results or empty list on error.
    """
    if not is_secure_url(JACKETT_API_URL):
        logger.error("Insecure JACKETT_API_URL: HTTPS or localhost is required")
        return []

    try:
        return await _fetch_results_from_jackett(indexer_id, query, jackett_cookie)
    except ValueError as e:
        logger.error(str(e))
        return []
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching from indexer {indexer_id}: {e.response.status_code}")
        return []
    except httpx.TimeoutException:
        logger.error(f"Timeout fetching from indexer {indexer_id}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error fetching from indexer {indexer_id}: {str(e)}")
        return []


async def stream_jackett_results_for_indexer(indexer_id: str, query: str, result_queue, jackett_cookie: str):
    """
    Fetch results from a single indexer and put them in the queue as they arrive.
    This allows for immediate streaming of individual results.
    """
    if not is_secure_url(JACKETT_API_URL):
        logger.error("Insecure JACKETT_API_URL: HTTPS or localhost is required")
        return

    try:
        results = await _fetch_results_from_jackett(indexer_id, query, jackett_cookie)

        # Put each result in the queue immediately
        for result in results:
            if isinstance(result, dict):
                await _enqueue_message(result_queue, indexer_id, "result", result)

        # Signal that this indexer is complete
        await _enqueue_message(result_queue, indexer_id, "indexer_complete", None)

    except ValueError as e:
        logger.error(str(e))
        # Logged as error, let's treat it as fetch error
        await _enqueue_message(result_queue, indexer_id, "indexer_error", {"error": str(e)})
    except Exception as e:
        logger.error(f"Failed to fetch from indexer {indexer_id}: {str(e)}")
        # Put error in queue
        await _enqueue_message(result_queue, indexer_id, "indexer_error", {"error": str(e)})


async def get_jackett_cookie_cached(force_refresh: bool = False) -> str | None:
    """
    Retrieves the 'Jackett' cookie from cache first, and if not available or force_refresh is True,
    fetches it using the authentication flow and caches it for future use.
    
    The cookie is cached for 14 days to match Jackett's cookie expiry period.
    
    Returns:
      The value of the 'Jackett' cookie, or None if the login fails or the cookie is not found.
    """
    if not force_refresh:
        cached_cookie = get_jackett_cookie_from_cache()
        if cached_cookie:
            logger.info("Using cached Jackett cookie")
            return cached_cookie

    async with _cookie_lock:
        if not force_refresh:
            cached_cookie = get_jackett_cookie_from_cache()
            if cached_cookie:
                logger.info("Using cached Jackett cookie")
                return cached_cookie

        logger.info(f"Fetching new Jackett cookie (force_refresh={force_refresh})")
        cookie = await get_jackett_cookie()
        if cookie:
            set_jackett_cookie_in_cache(cookie)

        return cookie


async def _perform_jackett_login_flow(dashboard_url: str, login_url: str) -> str | None:
    """
    Executes the multi-step login flow to obtain the Jackett cookie.
    """
    async with httpx.AsyncClient(follow_redirects=True, verify=True) as client:
        # Step 1: Request to Dashboard (will redirect to Login)
        logger.info("Step 1: Requesting Dashboard (expecting redirect to Login)")
        response = await client.get(dashboard_url)
        logger.debug(f"Dashboard response status: {response.status_code}")

        # Step 2: Request to Login with TestCookie
        logger.info("Step 2: Requesting Login with TestCookie")
        response = await client.get(login_url, cookies={"TestCookie": "1"})
        logger.debug(f"Login with TestCookie response status: {response.status_code}")

        # Step 3: Request to Login with cookiesChecked
        logger.info("Step 3: Requesting Login with cookiesChecked")
        response = await client.get(f"{login_url}?cookiesChecked=1")
        logger.debug(f"Login with cookiesChecked response status: {response.status_code}")

        # Step 4: Final request to Dashboard with Jackett cookie
        logger.info("Step 4: Final request to Dashboard with Jackett cookie")
        response = await client.get(dashboard_url)
        logger.debug(f"Final Dashboard response status: {response.status_code}")

        # Check for Jackett cookie in the client's cookie jar
        return client.cookies.get('Jackett')


async def get_jackett_cookie() -> str | None:
    """
    Retrieves the 'Jackett' cookie by following the authentication flow:
    1. Request to Dashboard (follows redirect to Login)
    2. Request to Login with TestCookie
    3. Request to Login with cookiesChecked
    4. Final request to Dashboard with Jackett cookie
    
    Returns:
      The value of the 'Jackett' cookie, or None if the login fails or the cookie is not found.
    """
    if not is_secure_url(JACKETT_API_URL):
        logger.error("Insecure JACKETT_API_URL: HTTPS or localhost is required for authentication")
        return None

    dashboard_url = f"{JACKETT_API_URL}/UI/Dashboard"
    login_url = f"{JACKETT_API_URL}/UI/Login"

    try:
        jackett_cookie = await _perform_jackett_login_flow(dashboard_url, login_url)

        if not jackett_cookie:
            logger.warning("Jackett cookie not found after authentication flow")
            return None
            
        logger.info("Successfully obtained Jackett cookie")
        return jackett_cookie
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error during Jackett authentication: {e.response.status_code}")
        return None
    except httpx.TimeoutException:
        logger.error("Timeout during Jackett authentication")
        return None
    except Exception as e:
        logger.error(f"Unexpected error during Jackett authentication: {str(e)}")
        return None
