
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import orjson
import asyncio
import logging
import time
from app.services.cache import get_configured_indexers_from_cache, set_configured_indexers_in_cache
from app.services.jackett_client import get_jackett_cookie, get_configured_indexers, stream_jackett_results_for_indexer
from app.services.utils import trimmed_result

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    generator_start_time = time.time()
    logger.info(f"🚀 EVENT GENERATOR START: query='{query}'")

    cache_start_time = time.time()
    configured_indexers = get_configured_indexers_from_cache()
    cache_time = (time.time() - cache_start_time) * 1000
    logger.info(f"💾 CACHE CHECK: {cache_time:.2f}ms, found {len(configured_indexers) if configured_indexers else 0} indexers")

    if not configured_indexers:
        auth_start_time = time.time()
        logger.info("🔐 AUTHENTICATION REQUIRED: fetching Jackett cookie")

        if not (jackett_cookie := await get_jackett_cookie()):
            auth_time = (time.time() - auth_start_time) * 1000
            logger.error(f"❌ AUTHENTICATION FAILED: {auth_time:.2f}ms")
            yield "event: error\ndata: Failed to authenticate with Jackett\n\n"
            return

        try:
            indexer_fetch_start = time.time()
            configured_indexers = await get_configured_indexers(jackett_cookie)
            indexer_fetch_time = (time.time() - indexer_fetch_start) * 1000
            logger.info(f"📋 INDEXERS FETCHED: {len(configured_indexers)} indexers in {indexer_fetch_time:.2f}ms")

            set_configured_indexers_in_cache(configured_indexers)
            auth_time = (time.time() - auth_start_time) * 1000
            logger.info(f"✅ AUTHENTICATION COMPLETE: {auth_time:.2f}ms total")
        except HTTPException as e:
            auth_time = (time.time() - auth_start_time) * 1000
            logger.error(f"❌ INDEXER FETCH FAILED: {auth_time:.2f}ms")
            yield f"event: error\ndata: {orjson.dumps(e.detail).decode()}\n\n"
            return

    # Create a queue to collect results from all indexers
    result_queue = asyncio.Queue()

    # Launch all indexers in parallel, each streaming to the queue
    parallel_start_time = time.time()
    logger.info(f"🔄 LAUNCHING PARALLEL INDEXERS: {len(configured_indexers)} indexers")

    tasks = []
    for indexer_id in configured_indexers:
        task_start = time.time()
        task = asyncio.create_task(stream_jackett_results_for_indexer(indexer_id, query, result_queue))
        tasks.append(task)
        task_creation_time = (time.time() - task_start) * 1000
        logger.info(f"📤 INDEXER TASK CREATED: {indexer_id} in {task_creation_time:.2f}ms")

    parallel_setup_time = (time.time() - parallel_start_time) * 1000
    logger.info(f"⚡ ALL TASKS LAUNCHED: {parallel_setup_time:.2f}ms")

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
            break

    # Cancel any remaining tasks
    for task in tasks:
        if not task.done():
            task.cancel()

    # Final status
    yield f"event: search_complete\ndata: {orjson.dumps({
        'completed': list(completed_indexers),
        'failed': list(failed_indexers)
    }).decode()}\n\n"



@app.get("/search")
async def search(query: str):
    request_start_time = time.time()
    logger.info(f"🔍 SEARCH REQUEST RECEIVED: query='{query}' at {request_start_time:.3f}")

    async def timed_event_generator():
        setup_start_time = time.time()
        logger.info(f"⚙️  SETUP PHASE START: {(setup_start_time - request_start_time) * 1000:.2f}ms after request")

        first_result_sent = False
        result_count = 0

        async for event in event_generator(query):
            if not first_result_sent and event.startswith("data:"):
                first_result_time = time.time()
                time_to_first_result = (first_result_time - request_start_time) * 1000
                logger.info(f"⚡ FIRST RESULT STREAMED: {time_to_first_result:.2f}ms after request")
                first_result_sent = True

            if event.startswith("data:"):
                result_count += 1

            yield event

        final_time = time.time()
        total_time = (final_time - request_start_time) * 1000
        logger.info(f"🏁 SEARCH COMPLETE: {result_count} results in {total_time:.2f}ms total")

    return StreamingResponse(timed_event_generator(), media_type="text/event-stream")

@app.get("/indexers")
async def get_indexers():
    configured_indexers = get_configured_indexers_from_cache()
    if configured_indexers:
        return JSONResponse(content={"indexers": configured_indexers})

    jackett_cookie = get_jackett_cookie()
    if not jackett_cookie:
        raise HTTPException(status_code=500, detail="Failed to retrieve Jackett cookie.")

    configured_indexers = await get_configured_indexers(jackett_cookie)
    set_configured_indexers_in_cache(configured_indexers)
    return JSONResponse(content={"indexers": configured_indexers})

@app.get("/")
async def root():
    return {"message": "Hello freeloader!!, feel free to use /search and /indexers"}
