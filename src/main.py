import logging

from fastapi import FastAPI, HTTPException

from src.config.settings import settings
from src.middleware.error_handler import generic_exception_handler, http_exception_handler
from src.routes import documents, health, search
from src.services import cache, database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

standalone = settings.STANDALONE_MODE

app = FastAPI(
    title="Distributed Document Search Service",
    description="Multi-tenant document search API with Elasticsearch, Redis caching, and async indexing via RabbitMQ.",
    version="1.0.0",
)

# Exception handlers
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)

# Routes
app.include_router(documents.router)
app.include_router(search.router)
app.include_router(health.router)


@app.on_event("startup")
async def startup():
    logger.info("Starting up — initializing SQLite database...")
    await database.init_db()

    if standalone:
        logger.info("Running in STANDALONE mode — ES, Redis, RabbitMQ disabled")
    else:
        from src.services import queue
        logger.info("Setting up RabbitMQ topology...")
        try:
            await queue.setup_topology()
            logger.info("RabbitMQ topology ready")
        except Exception as e:
            logger.error(f"Failed to set up RabbitMQ topology: {e}")


@app.on_event("shutdown")
async def shutdown():
    logger.info("Shutting down — closing connections...")
    await cache.close_redis_client()
    if not standalone:
        from src.services import elasticsearch, queue
        await elasticsearch.close_es_client()
        await queue.close_connection()
    logger.info("All connections closed")
