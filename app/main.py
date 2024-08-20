import asyncio
import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.encoders import jsonable_encoder
import logging
import json
from datetime import datetime, timedelta
import os
from fastapi.middleware.cors import CORSMiddleware
import time
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

origins = ["*"]  # Allow all origins (for testing)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
load_dotenv()  # Load environment variables from .env file

JACKETT_API_URL = os.getenv("JACKETT_API_URL")
API_KEY = os.getenv("API_KEY")
PORT = int(os.getenv("PORT", 8000))  # Default to 8000 if PORT is not set

CACHE_FILE = "configured_indexers.json"
CACHE_DURATION = timedelta(minutes=30)

# Initialize last cache update time
last_cache_update = datetime.now()

def get_search_results_url(indexer_id: str):
    return f"{JACKETT_API_URL}/api/v2.0/indexers/{indexer_id}/results"

async def fetch_jackett_results_for_indexer(session: aiohttp.ClientSession, indexer_id: str, query: str):
    url = get_search_results_url(indexer_id)
    params = {"apikey": API_KEY, "Query": query}
    try:
        async with session.get(url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                results = data.get("Results", [])
                logger.info(f"Indexer {indexer_id} returned {len(results)} results.")
                return [jsonable_encoder(trimmed_result(result)) for result in results]
            else:
                logger.error(f"Error fetching from indexer {indexer_id}: {response.status}")
                return [{"error": f"Error fetching from indexer {indexer_id}: {response.status}"}]
    except Exception as e:
        logger.error(f"Exception fetching from indexer {indexer_id}: {str(e)}")
        return [{"error": f"Exception fetching from indexer {indexer_id}: {str(e)}"}]

def create_magnet_link(result):
    torrenturl = result.get("Link")
    infohash = result.get("InfoHash")
    magneturi = result.get("MagnetUri")

    if magneturi is not None:
        return magneturi
    elif torrenturl is not None and infohash is not None:
        return f"magnet:?xt=urn:btih:{infohash.lower()}"
    else:
        return torrenturl

def trimmed_result(result):
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

async def process_indexer(session: aiohttp.ClientSession, indexer_id: str, query: str):
    start_time = time.time()
    logger.info(f"Starting query for indexer {indexer_id}")
    results = await fetch_jackett_results_for_indexer(session, indexer_id, query)
    end_time = time.time()
    logger.info(f"Finished query for indexer {indexer_id}. Time taken: {end_time - start_time:.2f} seconds")
    return results


async def get_configured_indexers_from_file():
    global last_cache_update
    if os.path.exists(CACHE_FILE) and datetime.now() - last_cache_update < CACHE_DURATION:
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Error reading cache file: {e}")

    logger.info("Cache is stale or doesn't exist. Fetching configured indexers from Jackett...")
    configured_indexers = await get_configured_indexers()
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(configured_indexers, f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Error saving cache file: {e}")

    last_cache_update = datetime.now()
    return configured_indexers

async def get_configured_indexers():
    async with aiohttp.ClientSession() as session:
        params = {"apikey": API_KEY}
        url = f"{JACKETT_API_URL}/api/v2.0/indexers"
        try:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    indexers_data = await response.json()
                    configured_indexers = [indexer["id"] for indexer in indexers_data if indexer.get("configured", False)]
                    return configured_indexers
                else:
                    error_message = f"Jackett API Error: {response.status} - {response.reason}"
                    logger.error(error_message)
                    raise HTTPException(status_code=response.status, detail=error_message)
        except aiohttp.ClientError as e:
            error_message = f"Error connecting to Jackett API: {str(e)}"
            logger.error(error_message)
            raise HTTPException(status_code=500, detail=error_message)


async def event_generator(query: str):
    configured_indexers = await get_configured_indexers_from_file()
    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(process_indexer(session, indexer_id, query)) for indexer_id in configured_indexers]
        for completed_task in asyncio.as_completed(tasks):
            results = await completed_task
            for item in results:
                yield f"data: {json.dumps(item)}\n\n"

@app.get("/search")
async def search(query: str):
    return StreamingResponse(event_generator(query), media_type="text/event-stream")

@app.get("/indexers")
async def get_indexers():
    configured_indexers = await get_configured_indexers()
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(configured_indexers, f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Error saving cache file: {e}")

    return JSONResponse(content={"indexers": configured_indexers})

@app.get("/")
async def root():
    return {"message": "Hello freeloader!!, feel free to use /search and /indexers"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)