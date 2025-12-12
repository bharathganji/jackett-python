from typing import Dict, Any, Optional

def create_magnet_link(result: Dict[str, Any]) -> Optional[str]:
    """
    Creates a magnet link from a result dictionary.
    """
    torrenturl = result.get("Link")
    infohash = result.get("InfoHash")
    magneturi = result.get("MagnetUri")
    if magneturi is not None:
        return magneturi
    elif torrenturl is not None and infohash is not None:
        return f"magnet:?xt=urn:btih:{infohash.lower()}"
    else:
        return torrenturl

def trimmed_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Trims and formats a result dictionary for API response.
    """
    return {
        "Title": result.get("Title"),
        "Link": create_magnet_link(result),
        "Size": result.get("Size"),
        "Seeders": result.get("Seeders"),
        "Leechers": result.get("Leechers"),
        "InfoHash": result.get("InfoHash"),
        "IndexerId": result.get("Tracker"),
        'year': result.get('Year'),
        "Details": result.get("Details"),
    }


class ResultDeduplicator:
    """
    Helper class to deduplicate search results based on InfoHash.
    """
    def __init__(self):
        self.seen_infohashes = set()

    def is_duplicate(self, result: Dict[str, Any]) -> bool:
        """
        Check if a result is a duplicate based on InfoHash.
        Returns True if duplicate, False if unique.
        """
        infohash = result.get("InfoHash")
        if infohash:
            if infohash in self.seen_infohashes:
                return True
            self.seen_infohashes.add(infohash)
        return False

    def reset(self):
        """Reset the deduplicator for a new search."""
        self.seen_infohashes = set()
