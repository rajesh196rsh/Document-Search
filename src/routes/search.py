import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.middleware.rate_limiter import check_rate_limit
from src.middleware.tenant import TenantContext
from src.models.schemas import SearchResponse
from src.services import cache, database

logger = logging.getLogger(__name__)

standalone = settings.STANDALONE_MODE

router = APIRouter(prefix="/v1", tags=["search"])


@router.get("/search", response_model=SearchResponse)
async def search_documents(
    q: str = Query(..., min_length=1, description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Results per page"),
    tags: str | None = Query(None, description="Comma-separated tag filter"),
    sort: str = Query("relevance", description="Sort by: relevance or date"),
    tenant: TenantContext = Depends(check_rate_limit),
    db: AsyncSession = Depends(database.get_db),
):
    filters_str = f"tags={tags or ''}"

    # Check cache first
    cached = await cache.get_cached_search(tenant.tenant_id, q, filters_str, page, size)
    if cached:
        return SearchResponse(
            query=q,
            total_hits=cached["total_hits"],
            page=page,
            size=size,
            took_ms=cached["took_ms"],
            results=cached["results"],
            facets=cached.get("facets", {}),
        )

    if standalone:
        # SQLite LIKE-based search
        result = await database.standalone_search(db, tenant.tenant_id, q, page, size, tags)
    else:
        # Elasticsearch
        from src.services import elasticsearch
        result = await elasticsearch.search_documents(
            tenant_id=tenant.tenant_id,
            query=q,
            page=page,
            size=size,
            tags=tags,
            sort=sort,
        )

    # Store in cache for next time
    await cache.set_cached_search(tenant.tenant_id, q, filters_str, page, size, result)

    return SearchResponse(
        query=q,
        total_hits=result["total_hits"],
        page=page,
        size=size,
        took_ms=result["took_ms"],
        results=result["results"],
        facets=result.get("facets", {}),
    )
