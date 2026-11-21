from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import app

client = TestClient(app)


def test_root_endpoint():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == "2.1.0"
    assert "/search" in data["endpoints"]
    assert "/indexers" in data["endpoints"]


def test_get_indexers_from_cache():
    cached_indexers = [
        {"id": "indexer1", "site_link": "https://indexer1.com"},
        {"id": "indexer2", "site_link": "https://indexer2.com"},
    ]
    with patch(
        "app.api.get_detailed_configured_indexers_from_cache",
        return_value=cached_indexers,
    ):
        response = client.get("/indexers")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["indexers"] == cached_indexers


def test_get_indexers_fetch_when_cache_empty():
    fetched_indexers = [{"id": "fresh_indexer", "site_link": "https://fresh.com"}]
    with patch("app.api.get_detailed_configured_indexers_from_cache", return_value=None), \
         patch("app.api.get_jackett_cookie_cached", new_callable=AsyncMock, return_value="valid_cookie"), \
         patch("app.api.get_detailed_configured_indexers", new_callable=AsyncMock, return_value=fetched_indexers), \
         patch("app.api.set_detailed_configured_indexers_in_cache") as mock_set_detailed, \
         patch("app.api.set_configured_indexers_in_cache") as mock_set_simple:

        response = client.get("/indexers")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["indexers"] == fetched_indexers
        mock_set_detailed.assert_called_once_with(fetched_indexers)
        mock_set_simple.assert_called_once_with(["fresh_indexer"])


def test_search_requires_query():
    response = client.get("/search?query=")
    assert response.status_code == 400
    assert "Query parameter is required" in response.json()["detail"]


def test_search_single_indexer_validation():
    response = client.get("/search/%20?query=ubuntu")
    assert response.status_code == 400
    assert "Indexer ID is required" in response.json()["detail"]

    response2 = client.get("/search/indexer1?query=%20")
    assert response2.status_code == 400
    assert "Query parameter is required" in response2.json()["detail"]


def test_search_multiple_validation():
    # Empty indexer_ids
    response = client.post("/search/multiple?query=ubuntu", json={"indexer_ids": []})
    assert response.status_code == 422  # Pydantic validation error min_length=1

    # Empty query
    response2 = client.post("/search/multiple?query=%20", json={"indexer_ids": ["idx1"]})
    assert response2.status_code == 400
    assert "Query parameter is required" in response2.json()["detail"]

    # Whitespace-only indexer_ids
    response3 = client.post("/search/multiple?query=ubuntu", json={"indexer_ids": ["  ", ""]})
    assert response3.status_code == 400
    assert "At least one valid indexer ID is required" in response3.json()["detail"]
