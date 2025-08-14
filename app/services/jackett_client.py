import httpx
from typing import Any, Optional
from fastapi import HTTPException
import logging
from pydantic_settings import BaseSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    JACKETT_API_URL: str
    API_KEY: str
    PORT: int = 8000

    class Config:
        env_file = ".env"


settings = Settings()
JACKETT_API_URL = settings.JACKETT_API_URL
API_KEY = settings.API_KEY


async def get_configured_indexers(jackett_cookie: str) -> list[str]:
    params = {"apikey": API_KEY, "configured": 'true'}
    headers = {'Cookie': f'Jackett={jackett_cookie}'}
    url = f"{JACKETT_API_URL}/api/v2.0/indexers"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code == 200:
                indexers_data = response.json()
                return [indexer["id"] for indexer in indexers_data if indexer.get("configured", False)]
            else:
                error_message = f"Jackett API Error: {response.status_code} - {response.reason_phrase}"
                raise HTTPException(
                    status_code=response.status_code, detail=error_message)
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=500, detail=f"Error connecting to Jackett API: {str(e)}")


async def fetch_jackett_results_for_indexer(indexer_id: str, query: str):
    url = f"{JACKETT_API_URL}/api/v2.0/indexers/{indexer_id}/results"
    params = {"apikey": API_KEY, "Query": query}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("Results", [])
    except Exception as e:
        logger.error(f"Failed to fetch from indexer {indexer_id}: {str(e)}")
        return [{"error": str(e)}]

async def stream_jackett_results_for_indexer(indexer_id: str, query: str, result_queue):
    """
    Fetch results from a single indexer and put them in the queue as they arrive.
    This allows for immediate streaming of individual results.
    """
    url = f"{JACKETT_API_URL}/api/v2.0/indexers/{indexer_id}/results"
    params = {"apikey": API_KEY, "Query": query}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            results = data.get("Results", [])

            # Put each result in the queue immediately
            for result in results:
                await result_queue.put({
                    "type": "result",
                    "indexer_id": indexer_id,
                    "data": result
                })

            # Signal that this indexer is complete
            await result_queue.put({
                "type": "indexer_complete",
                "indexer_id": indexer_id,
                "data": None
            })

    except Exception as e:
        logger.error(f"Failed to fetch from indexer {indexer_id}: {str(e)}")
        # Put error in queue
        await result_queue.put({
            "type": "indexer_error",
            "indexer_id": indexer_id,
            "data": {"error": str(e)}
        })

def get_jackett_cookie() -> Optional[str]:
    """
    Simulates a login to Pikpak Plus and retrieves the 'Jackett' cookie.
    Returns:
      The value of the 'Jackett' cookie, or None if the login fails or the cookie is not found.
    """
    test_cookie_url = f"{JACKETT_API_URL}/UI/Login?cookiesChecked=1"

    try:
        with httpx.Client() as client:
            response = client.get(test_cookie_url, cookies={"TestCookie": "1"})
            # Check for Jackett cookie in the response
            jackett_cookie = response.cookies.get('Jackett')
            if not jackett_cookie:
                print("Jackett cookie not found in session.")
                return None
            return jackett_cookie
    except Exception as e:
        print(f"Error during Jackett login: {e}")
        return None
