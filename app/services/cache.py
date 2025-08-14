from diskcache import Cache
from typing import Any

CACHE_DIR = "./cache_dir"
CACHE_KEY = "configured_indexers"
CACHE_DURATION = 86400  # seconds (30 minutes)
cache = Cache(CACHE_DIR)

def get_configured_indexers_from_cache() -> Any:
    """
    Loads configured indexers from diskcache.
    """
    return cache.get(CACHE_KEY)

def set_configured_indexers_in_cache(indexers: list[str]) -> None:
    """
    Saves configured indexers to diskcache.
    """
    cache.set(CACHE_KEY, indexers, expire=CACHE_DURATION)
