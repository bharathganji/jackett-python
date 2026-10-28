import ipaddress
import httpx
from typing import Optional, Dict, List
from urllib.parse import urlparse
from fastapi import HTTPException
import logging
from pydantic_settings import BaseSettings
from app.services.cache import (
    get_jackett_cookie_from_cache,
    set_jackett_cookie_in_cache
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants for error messages
INVALID_RESPONSE_FORMAT = "Invalid response format from Jackett API"
NO_CONFIGURED_INDEXERS = "No configured indexers found"


class Settings(BaseSettings):
    JACKETT_API_URL: str
    API_KEY: str
    PORT: int = 8000

    class Config:
        env_file = ".env"


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


async def fetch_indexers_data(jackett_cookie: str) -> List[Dict]:
    """
    Fetch raw indexer data from Jackett API.
    Common function used by get_configured_indexers and get_detailed_configured_indexers.
    """
    if not is_secure_url(JACKETT_API_URL):
        raise HTTPException(status_code=400, detail="Insecure JACKETT_API_URL: HTTPS or localhost is required to transmit credentials securely")

    if not jackett_cookie or not jackett_cookie.strip():
        raise HTTPException(status_code=400, detail="Jackett cookie is required")

    params = {"apikey": API_KEY, "configured": 'true'}
    headers = {
        'Cookie': f'Jackett={jackett_cookie.strip()}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    url = f"{JACKETT_API_URL}/api/v2.0/indexers"

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=True) as client:
            response = await client.get(url, params=params, headers=headers)

            # Check for redirect responses
            if response.is_redirect:
                logger.warning(f"Redirect detected when fetching indexers: {response.status_code}")
                # Follow the redirect manually
                redirect_url = response.headers.get('Location')
                if redirect_url:
                    logger.info(f"Following redirect to: {redirect_url}")
                    response = await client.get(redirect_url, params=params, headers=headers)

            # Ensure we have a successful response
            response.raise_for_status()

            indexers_data = response.json()
            if not isinstance(indexers_data, list):
                raise HTTPException(status_code=500, detail=INVALID_RESPONSE_FORMAT)

            return indexers_data

    except httpx.HTTPStatusError as e:
        error_message = f"Jackett API HTTP error: {e.response.status_code}"
        logger.error(error_message)
        raise HTTPException(status_code=e.response.status_code, detail=error_message)
    except httpx.TimeoutException:
        error_message = "Timeout connecting to Jackett API"
        logger.error(error_message)
        raise HTTPException(status_code=504, detail=error_message)
    except httpx.HTTPError as e:
        error_message = f"Error connecting to Jackett API: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message)
    except Exception as e:
        error_message = f"Unexpected error fetching indexers: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message)


async def get_configured_indexers(jackett_cookie: str) -> list[str]:
    indexers_data = await fetch_indexers_data(jackett_cookie)
    configured_indexers = [
        indexer["id"] for indexer in indexers_data
        if isinstance(indexer, dict) and indexer.get("configured", False) and indexer.get("id")
    ]
    if not configured_indexers:
        logger.warning(NO_CONFIGURED_INDEXERS)
    return configured_indexers


async def get_detailed_configured_indexers(jackett_cookie: str) -> List[Dict[str, str]]:
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


async def _fetch_results_from_jackett(indexer_id: str, query: str, jackett_cookie: str):
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

    async with httpx.AsyncClient(timeout=60, verify=True) as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()

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


async def fetch_jackett_results_for_indexer(indexer_id: str, query: str, jackett_cookie: str):
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

async def get_jackett_cookie_cached() -> Optional[str]:
    """
    Retrieves the 'Jackett' cookie from cache first, and if not available,
    fetches it using the authentication flow and caches it for future use.
    
    The cookie is cached for 14 days to match Jackett's cookie expiry period.
    
    Returns:
      The value of the 'Jackett' cookie, or None if the login fails or the cookie is not found.
    """
    # Try to get cached cookie first
    cached_cookie = get_jackett_cookie_from_cache()
    if cached_cookie:
        logger.info("Using cached Jackett cookie")
        return cached_cookie
    
    # If not in cache, fetch a new one
    logger.info("No cached cookie found, fetching new Jackett cookie")
    cookie = await get_jackett_cookie()
    if cookie:
        # Cache the newly fetched cookie
        set_jackett_cookie_in_cache(cookie)
    
    return cookie


async def _perform_jackett_login_flow(dashboard_url: str, login_url: str) -> Optional[str]:
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

async def get_jackett_cookie() -> Optional[str]:
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
