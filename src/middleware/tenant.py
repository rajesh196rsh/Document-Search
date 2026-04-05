import logging
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.database import get_db, get_tenant_by_id

logger = logging.getLogger(__name__)


class TenantContext:
    """Holds resolved tenant info for the current request."""

    def __init__(self, tenant_id: str, name: str, rate_limit: int, config: dict):
        self.tenant_id = tenant_id
        self.name = name
        self.rate_limit = rate_limit
        self.config = config


async def resolve_tenant(
    request: Request,
    x_tenant_id: str = Header(..., description="Tenant identifier"),
    db: AsyncSession = Depends(get_db),
) -> TenantContext:
    """
    FastAPI dependency that resolves and validates the tenant on every request.
    Attaches tenant context so downstream code never makes unscoped queries.
    """
    # Validate UUID format
    try:
        UUID(x_tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Tenant-ID must be a valid UUID")

    tenant = await get_tenant_by_id(db, x_tenant_id)

    if tenant is None:
        raise HTTPException(status_code=401, detail=f"Unknown tenant: {x_tenant_id}")

    if not tenant.get("is_active", False):
        raise HTTPException(status_code=403, detail=f"Tenant '{tenant['name']}' is deactivated")

    logger.debug(f"Resolved tenant: {tenant['name']} ({x_tenant_id})")

    return TenantContext(
        tenant_id=x_tenant_id,
        name=tenant["name"],
        rate_limit=tenant.get("rate_limit", 100),
        config=tenant.get("config", {}),
    )
