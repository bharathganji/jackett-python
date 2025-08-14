from diskcache import Cache
from typing import Any, Optional, List, Dict, Union
import logging
import os

logger = logging.getLogger(__name__)

CACHE_DIR = "./cache_dir"
CACHE_KEY = "configured_indexers"
CACHE_KEY_DETAILED = "configured_indexers_detailed"
CACHE_DURATION = 86400  # seconds (24 hours)

# Ensure cache directory exists
try:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = Cache(CACHE_DIR)
    # Test cache with a simple operation
    cache.set('test_key', 'test_value', expire=10)
    test_result = cache.get('test_key')
    if test_result == 'test_value':
        logger.info(f"Cache initialized and tested successfully at {CACHE_DIR}")
    else:
        logger.warning(f"Cache initialized but test failed at {CACHE_DIR}")
except Exception as e:
    logger.error(f"Failed to initialize cache: {str(e)}")
    cache = None

def get_configured_indexers_from_cache() -> Optional[list[str]]:
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
    except Exception as e:
        logger.error(f"Error reading from cache: {str(e)}")
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
    except Exception as e:
        logger.error(f"Error writing to cache: {str(e)}")
        return False


def get_detailed_configured_indexers_from_cache() -> Optional[List[Dict[str, str]]]:
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
            # Validate that each item is a dict with required keys
            if all(isinstance(item, dict) and "id" in item and "site_link" in item for item in result):
                logger.info(f"✅ Retrieved {len(result)} detailed indexers from cache")
                return result
            else:
                logger.warning("Cache data validation failed - invalid structure")
        else:
            logger.info("No valid detailed indexers found in cache")
        return None
    except Exception as e:
        logger.error(f"Error reading detailed indexers from cache: {str(e)}")
        return None


def set_detailed_configured_indexers_in_cache(indexers: List[Dict[str, str]]) -> bool:
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
        if verify_result and len(verify_result) == len(indexers):
            logger.info(f"✅ Successfully cached {len(indexers)} detailed indexers")
            return True
        else:
            logger.error("Cache write verification failed")
            return False
    except Exception as e:
        logger.error(f"Error writing detailed indexers to cache: {str(e)}")
        return False
