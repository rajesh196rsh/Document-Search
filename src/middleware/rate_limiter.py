import logging
import time
from collections import defaultdict

from fastapi import Depends, HTTPException

from src.config.settings import settings
from src.middleware.tenant import TenantContext, resolve_tenant
from src.services.cache import get_redis_client

logger = logging.getLogger(__name__)

standalone = settings.STANDALONE_MODE

WINDOW_SECONDS = 1

# In-memory rate limit store for standalone mode: {tenant_id: [timestamps]}
_mem_rate_store: dict[str, list[float]] = defaultdict(list)


async def _check_rate_limit_memory(tenant: TenantContext) -> None:
    """Simple in-memory sliding window rate limiter (standalone mode)."""
    now = time.time()
    window_start = now - WINDOW_SECONDS
    bucket = _mem_rate_store[tenant.tenant_id]

    # Prune old entries
    _mem_rate_store[tenant.tenant_id] = [t for t in bucket if t > window_start]
    bucket = _mem_rate_store[tenant.tenant_id]

    if len(bucket) >= tenant.rate_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Tenant '{tenant.name}' has exceeded {tenant.rate_limit} requests/second",
        )
    bucket.append(now)


async def _check_rate_limit_redis(tenant: TenantContext) -> None:
    """Redis sorted-set sliding window rate limiter."""
    key = f"ratelimit:{tenant.tenant_id}"
    now = time.time()
    window_start = now - WINDOW_SECONDS

    try:
        client = get_redis_client()
        pipe = client.pipeline()

        # Remove entries outside the window
        pipe.zremrangebyscore(key, 0, window_start)
        # Count entries in the current window
        pipe.zcard(key)
        # Add the current request
        pipe.zadd(key, {f"{now}": now})
        # Set expiry so keys don't stick around forever
        pipe.expire(key, WINDOW_SECONDS + 1)

        results = await pipe.execute()
        request_count = results[1]

        if request_count >= tenant.rate_limit:
            logger.warning(f"Rate limit hit for tenant {tenant.name}: {request_count}/{tenant.rate_limit} req/s")
            raise HTTPException(
                status_code=429,
                detail=f"Tenant '{tenant.name}' has exceeded {tenant.rate_limit} requests/second",
            )

    except HTTPException:
        raise
    except Exception as e:
        # If Redis is down, let the request through rather than blocking all traffic.
        # Availability over strictness — consistent with our AP-over-CP stance.
        logger.error(f"Rate limiter Redis error (allowing request): {e}")


async def check_rate_limit(tenant: TenantContext = Depends(resolve_tenant)) -> TenantContext:
    if standalone:
        await _check_rate_limit_memory(tenant)
    else:
        await _check_rate_limit_redis(tenant)
    return tenant
