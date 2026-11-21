from app.services.utils import ResultDeduplicator, create_magnet_link, trimmed_result


def test_create_magnet_link_prefers_magneturi():
    result = {
        "MagnetUri": "magnet:?xt=urn:btih:EXISTING_URI",
        "Link": "https://example.com/torrent.torrent",
        "InfoHash": "abcdef123456",
    }
    assert create_magnet_link(result) == "magnet:?xt=urn:btih:EXISTING_URI"


def test_create_magnet_link_constructs_from_infohash():
    result = {
        "Link": "https://example.com/torrent.torrent",
        "InfoHash": "ABCDEF123456",
    }
    assert create_magnet_link(result) == "magnet:?xt=urn:btih:abcdef123456"


def test_create_magnet_link_fallback_to_link():
    result = {
        "Link": "https://example.com/torrent.torrent",
    }
    assert create_magnet_link(result) == "https://example.com/torrent.torrent"


def test_create_magnet_link_none_when_empty():
    assert create_magnet_link({}) is None


def test_trimmed_result_formats_keys():
    raw = {
        "Title": "Linux ISO 24.04",
        "Link": "https://example.com/linux.torrent",
        "InfoHash": "hash123",
        "Size": 1024000,
        "Seeders": 50,
        "Leechers": 5,
        "Tracker": "ubuntu_tracker",
        "Year": 2024,
        "Details": "https://example.com/details/1",
    }
    trimmed = trimmed_result(raw)

    assert trimmed["Title"] == "Linux ISO 24.04"
    assert trimmed["Link"] == "magnet:?xt=urn:btih:hash123"
    assert trimmed["Size"] == 1024000
    assert trimmed["Seeders"] == 50
    assert trimmed["Leechers"] == 5
    assert trimmed["InfoHash"] == "hash123"
    assert trimmed["IndexerId"] == "ubuntu_tracker"
    assert trimmed["year"] == 2024
    assert trimmed["Details"] == "https://example.com/details/1"


def test_result_deduplicator():
    dedup = ResultDeduplicator()

    res1 = {"InfoHash": "unique_hash_1"}
    res2 = {"InfoHash": "unique_hash_2"}
    res1_dup = {"InfoHash": "unique_hash_1"}
    no_hash = {"Title": "No Hash Torrent"}

    assert not dedup.is_duplicate(res1)
    assert not dedup.is_duplicate(res2)
    assert dedup.is_duplicate(res1_dup)
    # Result without infohash is not treated as duplicate
    assert not dedup.is_duplicate(no_hash)

    # Test reset
    dedup.reset()
    assert not dedup.is_duplicate(res1)
