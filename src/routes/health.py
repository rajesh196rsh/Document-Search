import logging
import time
from datetime import datetime

from fastapi import APIRouter

from src.config.settings import settings
from src.models.schemas import DependencyHealth, HealthResponse
from src.services import cache, database

logger = logging.getLogger(__name__)

standalone = settings.STANDALONE_MODE

router = APIRouter(tags=["health"])

_start_time = time.time()


@router.get("/v1/health", response_model=HealthResponse)
async def health_check():
    """
    Probes every dependency and reports status.
    Returns 200 even if a dependency is down — the body tells the story.
    A load balancer can key off the top-level status field.
    """
    start = time.time()

    db_health = await database.check_health()
    db_ms = round((time.time() - start) * 1000, 1)

    start = time.time()
    redis_health = await cache.check_health()
    redis_ms = round((time.time() - start) * 1000, 1)

    deps = {
        "sqlite": DependencyHealth(
            status=db_health["status"], latency_ms=db_ms, error=db_health.get("error")
        ),
        "redis": DependencyHealth(
            status=redis_health["status"], latency_ms=redis_ms, error=redis_health.get("error")
        ),
    }

    if not standalone:
        from src.services import elasticsearch, queue

        start = time.time()
        es_health = await elasticsearch.check_health()
        es_ms = round((time.time() - start) * 1000, 1)

        start = time.time()
        rmq_health = await queue.check_health()
        rmq_ms = round((time.time() - start) * 1000, 1)

        deps["elasticsearch"] = DependencyHealth(
            status=es_health["status"], latency_ms=es_ms, error=es_health.get("error")
        )
        deps["rabbitmq"] = DependencyHealth(
            status=rmq_health["status"], latency_ms=rmq_ms, error=rmq_health.get("error")
        )

    all_ok = all(d.status in ("healthy", "skipped") for d in deps.values())

    return HealthResponse(
        status="healthy" if all_ok else "degraded",
        uptime_seconds=round(time.time() - _start_time, 1),
        timestamp=datetime.utcnow(),
        dependencies=deps,
    )
