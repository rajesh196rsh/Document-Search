import hashlib
import logging
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.middleware.rate_limiter import check_rate_limit
from src.middleware.tenant import TenantContext
from src.models.schemas import (
    DocumentCreateRequest,
    DocumentCreateResponse,
    DocumentDeleteResponse,
    DocumentResponse,
)
from src.config.settings import settings
from src.services import cache, database

logger = logging.getLogger(__name__)

standalone = settings.STANDALONE_MODE

router = APIRouter(prefix="/v1/documents", tags=["documents"])


@router.post("", response_model=DocumentCreateResponse, status_code=202)
async def create_document(
    body: DocumentCreateRequest,
    tenant: TenantContext = Depends(check_rate_limit),
    db: AsyncSession = Depends(database.get_db),
):
    doc_id = uuid4()
    content_hash = hashlib.sha256(body.content.encode()).hexdigest()

    # Write metadata to DB first (system of record, strong consistency)
    await database.insert_document_metadata(
        session=db,
        doc_id=doc_id,
        tenant_id=tenant.tenant_id,
        title=body.title,
        content_hash=content_hash,
        file_type=body.file_type,
        metadata=body.metadata,
    )

    payload = {
        "title": body.title,
        "content": body.content,
        "tags": body.tags,
        "author": body.author,
        "file_type": body.file_type,
        "tenant_id": tenant.tenant_id,
        "created_at": datetime.utcnow().isoformat(),
    }

    if standalone:
        # Index synchronously into SQLite
        await database.standalone_index_document(db, str(doc_id), tenant.tenant_id, payload)
    else:
        # Push to queue for async ES indexing
        from src.services import queue
        await queue.publish_index_event(
            tenant_id=tenant.tenant_id,
            document_id=str(doc_id),
            payload=payload,
        )

    # Invalidate search cache for this tenant
    await cache.invalidate_tenant_cache(tenant.tenant_id)

    status = "indexed" if standalone else "processing"
    message = "Document indexed" if standalone else "Document queued for indexing"

    return DocumentCreateResponse(
        id=doc_id,
        status=status,
        message=message,
        created_at=datetime.utcnow(),
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: UUID,
    tenant: TenantContext = Depends(check_rate_limit),
    db: AsyncSession = Depends(database.get_db),
):
    # Fetch metadata from DB (authoritative)
    doc_meta = await database.get_document_metadata(db, document_id, tenant.tenant_id)
    if not doc_meta:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    # Try to get full content
    if standalone:
        doc_content = await database.standalone_get_document(db, tenant.tenant_id, str(document_id))
    else:
        from src.services import elasticsearch
        doc_content = await elasticsearch.get_document(tenant.tenant_id, str(document_id))

    content = doc_content.get("content") if doc_content else None
    tags = doc_content.get("tags", []) if doc_content else []
    author = doc_content.get("author") if doc_content else None

    # SQLite stores metadata as a JSON string — parse it back to dict
    raw_meta = doc_meta.get("metadata", {})
    if isinstance(raw_meta, str):
        import json
        try:
            raw_meta = json.loads(raw_meta)
        except Exception:
            raw_meta = {}

    return DocumentResponse(
        id=doc_meta["id"],
        title=doc_meta["title"],
        content=content,
        tags=tags,
        author=author,
        file_type=doc_meta.get("file_type"),
        status=doc_meta["status"],
        metadata=raw_meta,
        created_at=doc_meta["created_at"],
        updated_at=doc_meta.get("updated_at"),
    )


@router.delete("/{document_id}", response_model=DocumentDeleteResponse)
async def delete_document(
    document_id: UUID,
    tenant: TenantContext = Depends(check_rate_limit),
    db: AsyncSession = Depends(database.get_db),
):
    # Verify the document exists and belongs to this tenant
    doc_meta = await database.get_document_metadata(db, document_id, tenant.tenant_id)
    if not doc_meta:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    # Delete from DB
    await database.delete_document_metadata(db, document_id, tenant.tenant_id)

    if standalone:
        await database.standalone_delete_document(db, str(document_id))
    else:
        from src.services import queue
        await queue.publish_delete_event(
            tenant_id=tenant.tenant_id,
            document_id=str(document_id),
        )

    # Invalidate search cache
    await cache.invalidate_tenant_cache(tenant.tenant_id)

    return DocumentDeleteResponse(
        id=document_id,
        status="deleted",
        message="Document deletion queued",
    )
