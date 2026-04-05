import hashlib
import json
import logging
from typing import Any

from cachetools import TTLCache

from src.config.settings import settings

logger = logging.getLogger(__name__)

standalone = settings.STANDALONE_MODE

# L1: In-memory TTL cache (per-instance, 5s TTL, max 10K entries)
_l1_cache = TTLCache(maxsize=10_000, ttl=5)

_redis_client = None

L2_TTL_SECONDS = 60


def get_redis_client():
    global _redis_client
    if standalone:
        return None
    if _redis_client is None:
        import redis.asyncio as redis
        _redis_client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
        )
    return _redis_client


async def close_redis_client() -> None:
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None


def _cache_key(tenant_id: str, query: str, filters: str, page: int, size: int) -> str:
    raw = f"{query}:{filters}:{page}:{size}"
    hashed = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"search:{tenant_id}:{hashed}"


async def get_cached_search(tenant_id: str, query: str, filters: str, page: int, size: int) -> dict | None:
    key = _cache_key(tenant_id, query, filters, page, size)

    # L1 check
    if key in _l1_cache:
        logger.debug(f"L1 cache hit: {key}")
        return _l1_cache[key]

    # L2 check (skip if no Redis)
    client = get_redis_client()
    if client:
        try:
            data = await client.get(key)
            if data:
                logger.debug(f"L2 cache hit: {key}")
                parsed = json.loads(data)
                _l1_cache[key] = parsed  # promote to L1
                return parsed
        except Exception as e:
            logger.warning(f"Redis GET failed for {key}: {e}")

    return None


async def set_cached_search(
    tenant_id: str, query: str, filters: str, page: int, size: int, result: dict
) -> None:
    key = _cache_key(tenant_id, query, filters, page, size)

    # L1
    _l1_cache[key] = result

    # L2 (skip if no Redis)
    client = get_redis_client()
    if client:
        try:
            await client.set(key, json.dumps(result, default=str), ex=L2_TTL_SECONDS)
        except Exception as e:
            logger.warning(f"Redis SET failed for {key}: {e}")


async def invalidate_tenant_cache(tenant_id: str) -> None:
    """Blow away all search cache entries for a tenant."""
    prefix = f"search:{tenant_id}:"

    # Clear L1 — iterate a snapshot of keys
    keys_to_remove = [k for k in _l1_cache if k.startswith(prefix)]
    for k in keys_to_remove:
        _l1_cache.pop(k, None)

    # Clear L2 via SCAN (not KEYS — won't block Redis)
    client = get_redis_client()
    if client:
        try:
            cursor = 0
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=f"{prefix}*", count=100)
                if keys:
                    await client.unlink(*keys)
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning(f"Redis cache invalidation failed for tenant {tenant_id}: {e}")


async def check_health() -> dict:
    if standalone:
        return {"status": "skipped"}
    try:
        client = get_redis_client()
        await client.ping()
        return {"status": "healthy"}
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}
