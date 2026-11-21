import logging
import os

from diskcache import Cache

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.getcwd(), "cache_dir")
CACHE_KEY = "configured_indexers"
CACHE_KEY_DETAILED = "configured_indexers_detailed"
CACHE_KEY_COOKIE = "jackett_cookie"
CACHE_DURATION = 86400  # seconds (24 hours)
CACHE_COOKIE_DURATION = 1209600  # seconds (14 days)

# In-memory cache for cookie to work with multiprocessing
_memory_cache: dict[str, str] = {}

# Ensure cache directory exists
try:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache: Cache | None = Cache(CACHE_DIR)
    # Test cache with a simple operation
    cache.set('test_key', 'test_value', expire=10)
    test_result = cache.get('test_key')
    if test_result == 'test_value':
        logger.info(f"Cache initialized and tested successfully at {CACHE_DIR}")
    else:
        logger.warning(f"Cache initialized but test failed at {CACHE_DIR}")
        cache = None
except Exception as e:  # noqa: BLE001
    logger.error(f"Failed to initialize cache: {e!s}")
    cache = None


def get_configured_indexers_from_cache() -> list[str] | None:
    """
    Loads configured indexers from diskcache.
    Returns None if cache is unavailable or key doesn't exist.
    """
    if not cache:
        logger.debug("Cache not available")
        return None

    try:
        result = cache.get(CACHE_KEY)
        if result and isinstance(result, list):
            logger.debug(f"Retrieved {len(result)} indexers from cache")
            return result
        return None
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error reading from cache: {e!s}")
        return None


def set_configured_indexers_in_cache(indexers: list[str]) -> bool:
    """
    Saves configured indexers to diskcache.
    Returns True if successful, False otherwise.
    """
    if not cache:
        logger.debug("Cache not available for writing")
        return False

    if not isinstance(indexers, list):
        logger.error("Invalid indexers data type for cache")
        return False

    try:
        cache.set(CACHE_KEY, indexers, expire=CACHE_DURATION)
        logger.debug(f"Cached {len(indexers)} indexers")
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error writing to cache: {e!s}")
        return False


def get_detailed_configured_indexers_from_cache() -> list[dict[str, str]] | None:
    """
    Loads detailed configured indexers from diskcache.
    Returns None if cache is unavailable or key doesn't exist.
    """
    if not cache:
        logger.warning("Cache not available for detailed indexers")
        return None

    try:
        logger.info(f"Attempting to read detailed indexers from cache key: {CACHE_KEY_DETAILED}")
        result = cache.get(CACHE_KEY_DETAILED)
        logger.info(f"Cache get result: {type(result)}, length: {len(result) if isinstance(result, list) else 'N/A'}")

        if result and isinstance(result, list):
            logger.info(f"Retrieved {len(result)} detailed indexers from cache")
            return result
        else:
            logger.info("No valid detailed indexers found in cache")
        return None
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error reading detailed indexers from cache: {e!s}")
        return None


def set_detailed_configured_indexers_in_cache(indexers: list[dict[str, str]]) -> bool:
    """
    Saves detailed configured indexers to diskcache.
    Returns True if successful, False otherwise.
    """
    if not cache:
        logger.warning("Cache not available for writing detailed indexers")
        return False

    if not isinstance(indexers, list):
        logger.error("Invalid detailed indexers data type for cache")
        return False

    # Validate structure
    if not all(isinstance(item, dict) and "id" in item and "site_link" in item for item in indexers):
        logger.error("Invalid detailed indexers structure for cache")
        return False

    try:
        logger.info(f"Attempting to cache {len(indexers)} detailed indexers with key: {CACHE_KEY_DETAILED}")
        cache.set(CACHE_KEY_DETAILED, indexers, expire=CACHE_DURATION)

        # Verify the write was successful
        verify_result = cache.get(CACHE_KEY_DETAILED)
        if isinstance(verify_result, list) and len(verify_result) == len(indexers):
            logger.info(f"Successfully cached {len(indexers)} detailed indexers")
            return True
        else:
            logger.error("Cache write verification failed")
            return False
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error writing detailed indexers to cache: {e!s}")
        return False


def get_jackett_cookie_from_cache() -> str | None:
    """
    Loads Jackett cookie from in-memory cache or diskcache fallback.
    Returns None if not found.
    """
    result = _memory_cache.get(CACHE_KEY_COOKIE)
    if result and isinstance(result, str):
        logger.info("Retrieved Jackett cookie from in-memory cache")
        return result

    if cache:
        try:
            disk_result = cache.get(CACHE_KEY_COOKIE)
            if disk_result and isinstance(disk_result, str):
                _memory_cache[CACHE_KEY_COOKIE] = disk_result
                logger.info("Retrieved Jackett cookie from diskcache")
                return disk_result
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error reading cookie from diskcache: {e!s}")

    logger.info("No cached cookie found in memory or diskcache")
    return None


def set_jackett_cookie_in_cache(cookie: str) -> bool:
    """
    Saves Jackett cookie to in-memory cache and diskcache.
    Returns True if successful, False otherwise.
    """
    if not isinstance(cookie, str):
        logger.error("Invalid cookie data type for cache")
        return False

    _memory_cache[CACHE_KEY_COOKIE] = cookie
    if cache:
        try:
            cache.set(CACHE_KEY_COOKIE, cookie, expire=CACHE_COOKIE_DURATION)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error writing cookie to diskcache: {e!s}")

    logger.info("Successfully cached Jackett cookie in memory and diskcache")
    return True


def clear_jackett_cookie_cache() -> None:
    """
    Clears Jackett cookie from in-memory cache and diskcache.
    """
    _memory_cache.pop(CACHE_KEY_COOKIE, None)
    if cache:
        try:
            cache.delete(CACHE_KEY_COOKIE)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error deleting cookie from diskcache: {e!s}")
    logger.info("Cleared Jackett cookie from cache")
