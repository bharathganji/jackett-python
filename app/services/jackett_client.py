import httpx
from typing import Any, Optional, Dict, List
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


async def fetch_indexers_data(jackett_cookie: str) -> List[Dict]:
    """
    Fetch raw indexer data from Jackett API.
    Common function used by get_configured_indexers and get_detailed_configured_indexers.
    """
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
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
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


async def fetch_jackett_results_for_indexer(indexer_id: str, query: str, jackett_cookie: str):
    """
    Fetch results from a single indexer.
    Returns a list of results or empty list on error.
    """
    if not indexer_id or not indexer_id.strip():
        logger.error("Invalid indexer_id provided")
        return []

    if not query or not query.strip():
        logger.error("Invalid query provided")
        return []

    if not jackett_cookie or not jackett_cookie.strip():
        logger.error("Invalid jackett_cookie provided")
        return []

    url = f"{JACKETT_API_URL}/api/v2.0/indexers/{indexer_id.strip()}/results"
    params = {"apikey": API_KEY, "Query": query.strip()}
    headers = {
        'Cookie': f'Jackett={jackett_cookie.strip()}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9'
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                logger.error(f"Invalid response format from indexer {indexer_id}")
                return []

            results = data.get("Results", [])
            if not isinstance(results, list):
                logger.error(f"Invalid results format from indexer {indexer_id}")
                return []

            return results

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
    if not indexer_id or not indexer_id.strip():
        logger.error("Invalid indexer_id provided")
        return

    if not query or not query.strip():
        logger.error("Invalid query provided")
        return

    if not jackett_cookie or not jackett_cookie.strip():
        logger.error("Invalid jackett_cookie provided")
        return

    url = f"{JACKETT_API_URL}/api/v2.0/indexers/{indexer_id.strip()}/results"
    params = {"apikey": API_KEY, "Query": query.strip()}
    headers = {
        'Cookie': f'Jackett={jackett_cookie.strip()}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9'
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                raise ValueError("Invalid response format from Jackett API")

            results = data.get("Results", [])
            if not isinstance(results, list):
                raise ValueError("Invalid results format from Jackett API")

            # Put each result in the queue immediately
            for result in results:
                if isinstance(result, dict):
                    try:
                        await result_queue.put({
                            "type": "result",
                            "indexer_id": indexer_id,
                            "data": result
                        })
                    except Exception as queue_error:
                        logger.error(f"Failed to put result in queue for {indexer_id}: {str(queue_error)}")

            # Signal that this indexer is complete
            try:
                await result_queue.put({
                    "type": "indexer_complete",
                    "indexer_id": indexer_id,
                    "data": None
                })
            except Exception as queue_error:
                logger.error(f"Failed to put completion signal in queue for {indexer_id}: {str(queue_error)}")

    except Exception as e:
        logger.error(f"Failed to fetch from indexer {indexer_id}: {str(e)}")
        # Put error in queue
        try:
            await result_queue.put({
                "type": "indexer_error",
                "indexer_id": indexer_id,
                "data": {"error": str(e)}
            })
        except Exception as queue_error:
            logger.error(f"Failed to put error in queue for {indexer_id}: {str(queue_error)}")

def get_jackett_cookie_cached() -> Optional[str]:
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
    cookie = get_jackett_cookie()
    if cookie:
        # Cache the newly fetched cookie
        set_jackett_cookie_in_cache(cookie)
    
    return cookie


def get_jackett_cookie() -> Optional[str]:
    """
    Retrieves the 'Jackett' cookie by following the authentication flow:
    1. Request to Dashboard (follows redirect to Login)
    2. Request to Login with TestCookie
    3. Request to Login with cookiesChecked
    4. Final request to Dashboard with Jackett cookie
    
    Returns:
      The value of the 'Jackett' cookie, or None if the login fails or the cookie is not found.
    """
    dashboard_url = f"{JACKETT_API_URL}/UI/Dashboard"
    login_url = f"{JACKETT_API_URL}/UI/Login"

    try:
        with httpx.Client(follow_redirects=True) as client:
            # Step 1: Request to Dashboard (will redirect to Login)
            logger.info("Step 1: Requesting Dashboard (expecting redirect to Login)")
            response = client.get(dashboard_url)
            logger.debug(f"Dashboard response status: {response.status_code}")

            # Step 2: Request to Login with TestCookie
            logger.info("Step 2: Requesting Login with TestCookie")
            response = client.get(login_url, cookies={"TestCookie": "1"})
            logger.debug(f"Login with TestCookie response status: {response.status_code}")

            # Step 3: Request to Login with cookiesChecked
            logger.info("Step 3: Requesting Login with cookiesChecked")
            response = client.get(f"{login_url}?cookiesChecked=1")
            logger.debug(f"Login with cookiesChecked response status: {response.status_code}")

            # Step 4: Final request to Dashboard with Jackett cookie
            logger.info("Step 4: Final request to Dashboard with Jackett cookie")
            response = client.get(dashboard_url)
            logger.debug(f"Final Dashboard response status: {response.status_code}")

            # Check for Jackett cookie in the client's cookie jar
            jackett_cookie = client.cookies.get('Jackett')
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
