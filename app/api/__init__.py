
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import orjson
import asyncio
import logging
from app.services.cache import (
    get_configured_indexers_from_cache,
    set_configured_indexers_in_cache,
    get_detailed_configured_indexers_from_cache,
    set_detailed_configured_indexers_in_cache
)
from app.services.jackett_client import (
    get_jackett_cookie,
    get_configured_indexers,
    get_detailed_configured_indexers,
    stream_jackett_results_for_indexer
)
from app.services.utils import trimmed_result

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_indexer_ids_from_cache():
    """
    Helper function to get indexer IDs from cache, trying both simple and detailed formats.
    Returns a list of indexer IDs or None if not found.
    """
    # Try simple format first
    simple_indexers = get_configured_indexers_from_cache()
    if simple_indexers:
        return simple_indexers

    # Try detailed format
    detailed_indexers = get_detailed_configured_indexers_from_cache()
    if detailed_indexers:
        return [indexer["id"] for indexer in detailed_indexers]

    return None

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
        configured_indexers = get_indexer_ids_from_cache()

        if not configured_indexers:
            jackett_cookie = get_jackett_cookie()
            if not jackett_cookie:
                logger.error("Failed to authenticate with Jackett")
                yield "event: error\ndata: Failed to authenticate with Jackett\n\n"
                return

            try:
                configured_indexers = await get_configured_indexers(jackett_cookie)
                set_configured_indexers_in_cache(configured_indexers)
            except HTTPException as e:
                logger.error(f"Failed to fetch indexers: {e.detail}")
                yield f"event: error\ndata: {orjson.dumps(e.detail).decode()}\n\n"
                return
            except Exception as e:
                logger.error(f"Unexpected error fetching indexers: {str(e)}")
                yield "event: error\ndata: Unexpected error occurred while fetching indexers\n\n"
                return

        # Create a queue to collect results from all indexers
        result_queue = asyncio.Queue()

        # Launch all indexers in parallel, each streaming to the queue
        tasks = []
        for indexer_id in configured_indexers:
            task = asyncio.create_task(stream_jackett_results_for_indexer(indexer_id, query, result_queue))
            tasks.append(task)

        completed_indexers = set()
        failed_indexers = set()
        total_indexers = len(configured_indexers)

        # Process results as they arrive in the queue
        while len(completed_indexers) + len(failed_indexers) < total_indexers:
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
                remaining_indexers = set(configured_indexers) - completed_indexers - failed_indexers
                for indexer_id in remaining_indexers:
                    failed_indexers.add(indexer_id)
                    yield f"event: indexer_error\ndata: {indexer_id}: Timeout\n\n"
                logger.warning(f"Search timeout: {len(remaining_indexers)} indexers timed out")
                break
            except Exception as e:
                logger.error(f"Unexpected error during search: {str(e)}")
                yield f"event: error\ndata: Unexpected error occurred during search\n\n"
                break

        # Cancel any remaining tasks
        for task in tasks:
            if not task.done():
                task.cancel()

        # Final status
        yield f"event: search_complete\ndata: {orjson.dumps({'completed': list(completed_indexers), 'failed': list(failed_indexers)}).decode()}\n\n"

    except Exception as e:
        logger.error(f"Critical error in event generator: {str(e)}")
        yield f"event: error\ndata: Critical error occurred\n\n"



@app.get("/search")
async def search(query: str):
    if not query or not query.strip():
        raise HTTPException(status_code=400, detail="Query parameter is required and cannot be empty")

    try:
        return StreamingResponse(event_generator(query.strip()), media_type="text/event-stream")
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
        jackett_cookie = get_jackett_cookie()
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
        # Validate indexer exists
        configured_indexers = get_indexer_ids_from_cache()
        if not configured_indexers:
            jackett_cookie = get_jackett_cookie()
            if not jackett_cookie:
                logger.error("Failed to authenticate with Jackett")
                yield "event: error\ndata: Failed to authenticate with Jackett\n\n"
                return

            try:
                configured_indexers = await get_configured_indexers(jackett_cookie)
                set_configured_indexers_in_cache(configured_indexers)
            except HTTPException as e:
                logger.error(f"Failed to fetch indexers: {e.detail}")
                yield f"event: error\ndata: {orjson.dumps(e.detail).decode()}\n\n"
                return
            except Exception as e:
                logger.error(f"Unexpected error fetching indexers: {str(e)}")
                yield "event: error\ndata: Unexpected error occurred while fetching indexers\n\n"
                return

        if indexer_id not in configured_indexers:
            logger.error(f"Indexer not found: {indexer_id}")
            yield f"event: error\ndata: Indexer '{indexer_id}' not found or not configured\n\n"
            return

        # Create a queue for this single indexer
        result_queue = asyncio.Queue()

        # Launch the single indexer task
        task = asyncio.create_task(stream_jackett_results_for_indexer(indexer_id, query, result_queue))

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
        yield f"event: error\ndata: Critical error occurred\n\n"


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



@app.get("/")
async def root():
    return {
        "message": "Jackett Python API - Enhanced",
        "version": "2.0",
        "endpoints": {
            "/search": "Search all configured indexers",
            "/search/{indexer_id}": "Search a specific indexer",
            "/indexers": "Get detailed list of configured indexers with site links"
        },
        "enhancements": {
            "indexers_endpoint": "Now returns [{'id': 'indexer_id', 'site_link': 'https://...'}] instead of just IDs",
            "backward_compatibility": "All search functions remain unchanged",
            "caching": "Enhanced caching with detailed indexer information"
        }
    }
