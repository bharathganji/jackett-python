
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import orjson
import asyncio
import logging
from app.services.cache import (
    get_configured_indexers_from_cache,
    set_configured_indexers_in_cache,
    get_detailed_configured_indexers_from_cache,
    set_detailed_configured_indexers_in_cache,
    get_jackett_cookie_from_cache,
    set_jackett_cookie_in_cache
)
from app.services.jackett_client import (
    get_jackett_cookie_cached,
    get_configured_indexers,
    get_detailed_configured_indexers,
    stream_jackett_results_for_indexer
)
from app.services.utils import trimmed_result, ResultDeduplicator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants for error messages and media types
AUTHENTICATION_ERROR = "Failed to authenticate with Jackett"
INDEXER_FETCH_ERROR = "Unexpected error occurred while fetching indexers"
SEARCH_ERROR = "Unexpected error occurred during search"
CRITICAL_ERROR = "Critical error occurred"
QUERY_REQUIRED_ERROR = "Query parameter is required and cannot be empty"
STREAM_MEDIA_TYPE = "text/event-stream"


def get_indexer_ids_from_cache():
    """
    Helper function to get indexer IDs from cache using detailed format.
    Returns a list of indexer IDs or None if not found.
    """
    # Use detailed format only for consistency
    detailed_indexers = get_detailed_configured_indexers_from_cache()
    if detailed_indexers:
        return [indexer["id"] for indexer in detailed_indexers]

    return None


async def ensure_jackett_auth_and_indexers():
    """
    Common helper for authentication and indexer fetching/caching.
    Returns (jackett_cookie, configured_indexers) or raises AuthError with error message.
    Used by all event generators to avoid duplication.
    """
    jackett_cookie = get_jackett_cookie_cached()
    if not jackett_cookie:
        raise AuthError(AUTHENTICATION_ERROR)

    configured_indexers = get_indexer_ids_from_cache()

    if not configured_indexers:
        try:
            configured_indexers = await get_configured_indexers(jackett_cookie)
            set_configured_indexers_in_cache(configured_indexers)
        except HTTPException as e:
            logger.error(f"Failed to fetch indexers: {e.detail}")
            raise AuthError(orjson.dumps(e.detail).decode())
        except Exception as e:
            logger.error(f"Unexpected error fetching indexers: {str(e)}")
            raise AuthError(INDEXER_FETCH_ERROR)

    return jackett_cookie, configured_indexers


class AuthError(Exception):
    """Custom exception for authentication/indexer errors in generators"""
    pass

# Pydantic models
class MultipleIndexerSearchRequest(BaseModel):
    indexer_ids: List[str]

app = FastAPI()
origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)   



async def event_generator(query: str):
    """Stream individual results from all indexers in parallel as they arrive"""
    try:
        try:
            jackett_cookie, configured_indexers = await ensure_jackett_auth_and_indexers()
        except AuthError as e:
            yield f"event: error\ndata: {e}\n\n"
            return

        # Initialize result deduplicator
        deduplicator = ResultDeduplicator()

        # Create a queue to collect results from all indexers
        result_queue = asyncio.Queue()

        # Launch all indexers in parallel, each streaming to the queue
        tasks = []
        for indexer_id in configured_indexers:
            task = asyncio.create_task(stream_jackett_results_for_indexer(indexer_id, query, result_queue, jackett_cookie))
            tasks.append(task)

        completed_indexers = set()
        failed_indexers = set()
        total_indexers = len(configured_indexers)
        results_streamed = 0

        # Process results as they arrive in the queue
        while len(completed_indexers) + len(failed_indexers) < total_indexers:
            try:
                # Wait for next result with timeout to avoid hanging
                item = await asyncio.wait_for(result_queue.get(), timeout=120.0)

                if item["type"] == "result":
                    # Check for duplicates before streaming
                    if not deduplicator.is_duplicate(item["data"]):
                        result_data = trimmed_result(item["data"])
                        result_data["IndexerId"] = item["indexer_id"]  # Ensure indexer ID is included
                        yield f"data: {orjson.dumps(result_data).decode()}\n\n"
                        results_streamed += 1

                elif item["type"] == "indexer_complete":
                    completed_indexers.add(item["indexer_id"])
                    yield f"event: indexer_complete\ndata: {item['indexer_id']}\n\n"

                elif item["type"] == "indexer_error":
                    failed_indexers.add(item["indexer_id"])
                    yield f"event: indexer_error\ndata: {item['indexer_id']}: {item['data']['error']}\n\n"

            except asyncio.TimeoutError:
                # If we timeout, consider remaining indexers as failed
                remaining_indexers = set(configured_indexers) - completed_indexers - failed_indexers
                for indexer_id in remaining_indexers:
                    failed_indexers.add(indexer_id)
                    yield f"event: indexer_error\ndata: {indexer_id}: Timeout\n\n"
                logger.warning(f"Search timeout: {len(remaining_indexers)} indexers timed out")
                break
            except Exception as e:
                logger.error(f"Unexpected error during search: {str(e)}")
                yield "event: error\ndata: " + SEARCH_ERROR + "\n\n"
                break

        # Cancel any remaining tasks
        for task in tasks:
            if not task.done():
                task.cancel()

        # Final status with results count
        yield f"event: search_complete\ndata: {orjson.dumps({'completed': list(completed_indexers), 'failed': list(failed_indexers), 'results_count': results_streamed}).decode()}\n\n"

    except Exception as e:
        logger.error(f"Critical error in event generator: {str(e)}")
        yield "event: error\ndata: " + CRITICAL_ERROR + "\n\n"


async def multiple_indexers_event_generator(indexer_ids: List[str], query: str):
    """Stream individual results from specified indexers in parallel as they arrive"""
    try:
        try:
            jackett_cookie, configured_indexers = await ensure_jackett_auth_and_indexers()
        except AuthError as e:
            yield f"event: error\ndata: {e}\n\n"
            return

        # Validate that all requested indexers exist
        invalid_indexers = []
        valid_indexers = []
        
        for indexer_id in indexer_ids:
            if indexer_id in configured_indexers:
                valid_indexers.append(indexer_id)
            else:
                invalid_indexers.append(indexer_id)

        # Report invalid indexers as errors
        if invalid_indexers:
            for invalid_id in invalid_indexers:
                yield f"event: indexer_error\ndata: {invalid_id}: Indexer not found or not configured\n\n"
            logger.warning(f"Invalid indexers requested: {invalid_indexers}")

        # If no valid indexers, stop here
        if not valid_indexers:
            yield f"event: search_complete\ndata: {orjson.dumps({'completed': [], 'failed': invalid_indexers, 'message': 'No valid indexers provided'}).decode()}\n\n"
            return

        # Create a queue to collect results from valid indexers
        result_queue = asyncio.Queue()

        # Launch valid indexers in parallel, each streaming to the queue
        tasks = []
        for indexer_id in valid_indexers:
            task = asyncio.create_task(stream_jackett_results_for_indexer(indexer_id, query, result_queue, jackett_cookie))
            tasks.append(task)

        completed_indexers = set()
        failed_indexers = set(invalid_indexers)  # Start with invalid indexers as failed
        total_valid_indexers = len(valid_indexers)

        # Process results as they arrive in the queue
        while len(completed_indexers) < total_valid_indexers and len(completed_indexers) + len(failed_indexers) - len(invalid_indexers) < total_valid_indexers:
            try:
                # Wait for next result with timeout to avoid hanging
                item = await asyncio.wait_for(result_queue.get(), timeout=120.0)

                if item["type"] == "result":
                    # Stream individual result immediately
                    result_data = trimmed_result(item["data"])
                    result_data["IndexerId"] = item["indexer_id"]  # Ensure indexer ID is included
                    yield f"data: {orjson.dumps(result_data).decode()}\n\n"

                elif item["type"] == "indexer_complete":
                    completed_indexers.add(item["indexer_id"])
                    yield f"event: indexer_complete\ndata: {item['indexer_id']}\n\n"

                elif item["type"] == "indexer_error":
                    failed_indexers.add(item["indexer_id"])
                    yield f"event: indexer_error\ndata: {item['indexer_id']}: {item['data']['error']}\n\n"

            except asyncio.TimeoutError:
                # If we timeout, consider remaining indexers as failed
                remaining_indexers = set(valid_indexers) - completed_indexers - (failed_indexers - set(invalid_indexers))
                for indexer_id in remaining_indexers:
                    failed_indexers.add(indexer_id)
                    yield f"event: indexer_error\ndata: {indexer_id}: Timeout\n\n"
                logger.warning(f"Multiple indexer search timeout: {len(remaining_indexers)} indexers timed out")
                break
            except Exception as e:
                logger.error(f"Unexpected error during multiple indexer search: {str(e)}")
                yield "event: error\ndata: " + SEARCH_ERROR + "\n\n"
                break

        # Cancel any remaining tasks
        for task in tasks:
            if not task.done():
                task.cancel()

        # Final status
        yield f"event: search_complete\ndata: {orjson.dumps({'completed': list(completed_indexers), 'failed': list(failed_indexers)}).decode()}\n\n"

    except Exception as e:
        logger.error(f"Critical error in multiple indexers generator: {str(e)}")
        yield "event: error\ndata: " + CRITICAL_ERROR + "\n\n"


@app.get("/search")
async def search(query: str):
    if not query or not query.strip():
        raise HTTPException(status_code=400, detail=QUERY_REQUIRED_ERROR)

    try:
        return StreamingResponse(event_generator(query.strip()), media_type=STREAM_MEDIA_TYPE)
    except Exception as e:
        logger.error(f"Error in search endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred during search")

@app.get("/indexers")
async def get_indexers():
    """
    Get configured indexers with detailed information including id and site_link.
    Returns: {
        "indexers": [{"id": "indexer_id", "site_link": "https://example.com"}, ...],
        "total": 5
    }
    """
    try:
        # Try to get detailed indexers from cache first
        detailed_indexers = get_detailed_configured_indexers_from_cache()
        if detailed_indexers:
            return JSONResponse(content={
                "indexers": detailed_indexers,
                "total": len(detailed_indexers)
            })

        # If not in cache, fetch from Jackett API
        jackett_cookie = get_jackett_cookie_cached()
        if not jackett_cookie:
            raise HTTPException(status_code=500, detail="Failed to retrieve Jackett cookie")

        # Get detailed indexer information
        detailed_indexers = await get_detailed_configured_indexers(jackett_cookie)

        # Cache the detailed information
        set_detailed_configured_indexers_in_cache(detailed_indexers)

        # Also cache the simple list for backward compatibility with search functions
        simple_indexers = [indexer["id"] for indexer in detailed_indexers]
        set_configured_indexers_in_cache(simple_indexers)

        return JSONResponse(content={
            "indexers": detailed_indexers,
            "total": len(detailed_indexers)
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in indexers endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred while fetching indexers")

async def single_indexer_event_generator(indexer_id: str, query: str):
    """Stream results from a single indexer"""
    try:
        try:
            jackett_cookie, configured_indexers = await ensure_jackett_auth_and_indexers()
        except AuthError as e:
            yield f"event: error\ndata: {e}\n\n"
            return

        if indexer_id not in configured_indexers:
            logger.error(f"Indexer not found: {indexer_id}")
            yield f"event: error\ndata: Indexer '{indexer_id}' not found or not configured\n\n"
            return

        # Create a queue for this single indexer
        result_queue = asyncio.Queue()

        # Launch the single indexer task
        task = asyncio.create_task(stream_jackett_results_for_indexer(indexer_id, query, result_queue, jackett_cookie))

        results_streamed = 0
        indexer_completed = False
        indexer_failed = False

        # Process results as they arrive
        while not indexer_completed and not indexer_failed:
            try:
                # Wait for next result with timeout
                item = await asyncio.wait_for(result_queue.get(), timeout=120.0)

                if item["type"] == "result":
                    # Stream individual result immediately
                    result_data = trimmed_result(item["data"])
                    result_data["IndexerId"] = item["indexer_id"]
                    yield f"data: {orjson.dumps(result_data).decode()}\n\n"
                    results_streamed += 1

                elif item["type"] == "indexer_complete":
                    indexer_completed = True
                    yield f"event: indexer_complete\ndata: {item['indexer_id']}\n\n"

                elif item["type"] == "indexer_error":
                    indexer_failed = True
                    yield f"event: indexer_error\ndata: {item['indexer_id']}: {item['data']['error']}\n\n"

            except asyncio.TimeoutError:
                indexer_failed = True
                yield f"event: indexer_error\ndata: {indexer_id}: Timeout\n\n"
                logger.warning(f"Single indexer search timeout: {indexer_id}")
                break
            except Exception as e:
                indexer_failed = True
                logger.error(f"Error processing results from {indexer_id}: {str(e)}")
                yield f"event: indexer_error\ndata: {indexer_id}: Processing error\n\n"
                break

        # Cancel the task if it's still running
        if not task.done():
            task.cancel()

        # Final status
        status = "completed" if indexer_completed else "failed"
        yield f"event: search_complete\ndata: {orjson.dumps({'indexer': indexer_id, 'status': status, 'results_count': results_streamed}).decode()}\n\n"

    except Exception as e:
        logger.error(f"Critical error in single indexer generator: {str(e)}")
        yield "event: error\ndata: " + CRITICAL_ERROR + "\n\n"


@app.get("/search/{indexer_id}")
async def search_single_indexer(indexer_id: str, query: str):
    """Search a specific indexer"""
    if not indexer_id or not indexer_id.strip():
        raise HTTPException(status_code=400, detail="Indexer ID is required and cannot be empty")

    if not query or not query.strip():
        raise HTTPException(status_code=400, detail="Query parameter is required and cannot be empty")

    try:
        return StreamingResponse(
            single_indexer_event_generator(indexer_id.strip(), query.strip()),
            media_type="text/event-stream"
        )
    except Exception as e:
        logger.error(f"Error in single indexer search endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred during single indexer search")


@app.post("/search/multiple")
async def search_multiple_indexers(query: str, request: MultipleIndexerSearchRequest):
    """Search multiple specific indexers"""
    # Validate input
    if not request.indexer_ids or len(request.indexer_ids) == 0:
        raise HTTPException(status_code=400, detail="At least one indexer ID is required")
    
    if not query or not query.strip():
        raise HTTPException(status_code=400, detail="Query parameter is required and cannot be empty")
    
    # Remove duplicates and empty/whitespace-only IDs
    unique_indexer_ids = list({idx.strip() for idx in request.indexer_ids if idx and idx.strip()})
    
    if not unique_indexer_ids:
        raise HTTPException(status_code=400, detail="At least one valid indexer ID is required")
    
    try:
        return StreamingResponse(
            multiple_indexers_event_generator(unique_indexer_ids, query.strip()),
            media_type="text/event-stream"
        )
    except Exception as e:
        logger.error(f"Error in multiple indexer search endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred during multiple indexer search")


@app.get("/")
async def root():
    return {
        "message": "Jackett Python API - Enhanced",
        "version": "2.1",
        "endpoints": {
            "/search": "Search all configured indexers",
            "/search/{indexer_id}": "Search a specific indexer",
            "/search/multiple": "Search multiple specific indexers (POST with JSON body)",
            "/indexers": "Get detailed list of configured indexers with site links"
        },
        "enhancements": {
            "multiple_indexer_search": "New POST endpoint at /search/multiple accepts array of indexer IDs",
            "indexers_endpoint": "Returns [{'id': 'indexer_id', 'site_link': 'https://...'}] instead of just IDs",
            "backward_compatibility": "All existing search functions remain unchanged",
            "caching": "Enhanced caching with detailed indexer information"
        },
        "usage_examples": {
            "multiple_search": {
                "method": "POST",
                "url": "/search/multiple?query=your+search+query",
                "body": {
                    "indexer_ids": ["indexer1", "indexer2", "indexer3"]
                },
                "description": "Search only the specified indexers with query in URL parameter"
            }
        }
    }
