from typing import Any


def create_magnet_link(result: dict[str, Any]) -> str | None:
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


def trimmed_result(result: dict[str, Any]) -> dict[str, Any]:
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
        "year": result.get("Year"),
        "Details": result.get("Details"),
    }


class ResultDeduplicator:
    """
    Helper class to deduplicate search results based on InfoHash.
    """

    def __init__(self) -> None:
        self.seen_infohashes: set[str] = set()

    def is_duplicate(self, result: dict[str, Any]) -> bool:
        """
        Check if a result is a duplicate based on InfoHash.
        Returns True if duplicate, False if unique.
        """
        infohash = result.get("InfoHash")
        if infohash and isinstance(infohash, str):
            if infohash in self.seen_infohashes:
                return True
            self.seen_infohashes.add(infohash)
        return False

    def reset(self) -> None:
        """Reset the deduplicator for a new search."""
        self.seen_infohashes = set()
