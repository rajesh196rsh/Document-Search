import logging
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config.settings import settings

logger = logging.getLogger(__name__)

# Ensure the data directory exists before creating the engine
db_path = Path(settings.SQLITE_DB_PATH)
db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(
    settings.database_url,
    echo=False,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create tables and seed demo tenants. Safe to call multiple times."""
    async with async_session() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS tenants (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                api_key     TEXT NOT NULL,
                rate_limit  INTEGER DEFAULT 100,
                is_active   INTEGER DEFAULT 1,
                config      TEXT DEFAULT '{}',
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )
        """))

        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS documents (
                id            TEXT PRIMARY KEY,
                tenant_id     TEXT NOT NULL REFERENCES tenants(id),
                title         TEXT NOT NULL,
                content_hash  TEXT,
                file_type     TEXT,
                status        TEXT DEFAULT 'processing',
                metadata      TEXT DEFAULT '{}',
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now'))
            )
        """))

        # Indexes
        await session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_documents_tenant ON documents(tenant_id)"
        ))
        await session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(tenant_id, status)"
        ))
        await session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(tenant_id, created_at DESC)"
        ))

        # Document contents table — used in standalone mode for SQLite-based search
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS document_contents (
                id          TEXT PRIMARY KEY REFERENCES documents(id),
                tenant_id   TEXT NOT NULL,
                title       TEXT NOT NULL,
                content     TEXT,
                tags        TEXT DEFAULT '[]',
                author      TEXT,
                file_type   TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """))

        # Seed demo tenants (INSERT OR IGNORE so it's idempotent)
        await session.execute(text("""
            INSERT OR IGNORE INTO tenants (id, name, api_key, rate_limit)
            VALUES ('a1b2c3d4-e5f6-7890-abcd-ef1234567890', 'acme-corp', 'sk_acme_test_key_001', 100)
        """))
        await session.execute(text("""
            INSERT OR IGNORE INTO tenants (id, name, api_key, rate_limit)
            VALUES ('b2c3d4e5-f6a7-8901-bcde-f12345678901', 'globex-inc', 'sk_globex_test_key_002', 50)
        """))

        await session.commit()
        logger.info("SQLite database initialized — tables and demo tenants ready")


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session


async def check_health() -> dict:
    try:
        async with async_session() as session:
            result = await session.execute(text("SELECT 1"))
            result.scalar()
        return {"status": "healthy"}
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}


async def get_tenant_by_id(session: AsyncSession, tenant_id: str) -> dict | None:
    result = await session.execute(
        text("SELECT id, name, api_key, rate_limit, is_active, config FROM tenants WHERE id = :tid"),
        {"tid": tenant_id},
    )
    row = result.mappings().first()
    if row:
        return dict(row)
    return None


async def get_tenant_by_api_key(session: AsyncSession, api_key: str) -> dict | None:
    result = await session.execute(
        text("SELECT id, name, api_key, rate_limit, is_active, config FROM tenants WHERE api_key = :key"),
        {"key": api_key},
    )
    row = result.mappings().first()
    if row:
        return dict(row)
    return None


async def insert_document_metadata(
    session: AsyncSession,
    doc_id: UUID,
    tenant_id: str,
    title: str,
    content_hash: str | None,
    file_type: str | None,
    metadata: dict,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO documents (id, tenant_id, title, content_hash, file_type, status, metadata)
            VALUES (:id, :tenant_id, :title, :content_hash, :file_type, 'processing', :metadata)
            """
        ),
        {
            "id": str(doc_id),
            "tenant_id": tenant_id,
            "title": title,
            "content_hash": content_hash,
            "file_type": file_type,
            "metadata": str(metadata).replace("'", '"') if metadata else "{}",
        },
    )
    await session.commit()


async def update_document_status(session: AsyncSession, doc_id: UUID, status: str) -> None:
    await session.execute(
        text("UPDATE documents SET status = :status, updated_at = datetime('now') WHERE id = :id"),
        {"status": status, "id": str(doc_id)},
    )
    await session.commit()


async def get_document_metadata(session: AsyncSession, doc_id: UUID, tenant_id: str) -> dict | None:
    result = await session.execute(
        text(
            """
            SELECT id, tenant_id, title, content_hash, file_type, status, metadata, created_at, updated_at
            FROM documents WHERE id = :id AND tenant_id = :tenant_id
            """
        ),
        {"id": str(doc_id), "tenant_id": tenant_id},
    )
    row = result.mappings().first()
    if row:
        return dict(row)
    return None


async def delete_document_metadata(session: AsyncSession, doc_id: UUID, tenant_id: str) -> bool:
    result = await session.execute(
        text("DELETE FROM documents WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": str(doc_id), "tenant_id": tenant_id},
    )
    await session.commit()
    return result.rowcount > 0


# --- Standalone mode helpers (SQLite-based search, no ES needed) ---

async def standalone_index_document(
    session: AsyncSession, doc_id: str, tenant_id: str, payload: dict
) -> None:
    import json
    await session.execute(
        text("""
            INSERT OR REPLACE INTO document_contents (id, tenant_id, title, content, tags, author, file_type, created_at)
            VALUES (:id, :tenant_id, :title, :content, :tags, :author, :file_type, :created_at)
        """),
        {
            "id": doc_id,
            "tenant_id": tenant_id,
            "title": payload.get("title", ""),
            "content": payload.get("content", ""),
            "tags": json.dumps(payload.get("tags", [])),
            "author": payload.get("author"),
            "file_type": payload.get("file_type"),
            "created_at": payload.get("created_at"),
        },
    )
    await session.commit()
    # Also mark as indexed in documents table
    await update_document_status(session, doc_id, "indexed")


async def standalone_delete_document(session: AsyncSession, doc_id: str) -> None:
    await session.execute(
        text("DELETE FROM document_contents WHERE id = :id"), {"id": doc_id}
    )
    await session.commit()


async def standalone_get_document(session: AsyncSession, tenant_id: str, doc_id: str) -> dict | None:
    import json
    result = await session.execute(
        text("""
            SELECT id, tenant_id, title, content, tags, author, file_type, created_at
            FROM document_contents WHERE id = :id AND tenant_id = :tenant_id
        """),
        {"id": doc_id, "tenant_id": tenant_id},
    )
    row = result.mappings().first()
    if row:
        d = dict(row)
        try:
            d["tags"] = json.loads(d.get("tags", "[]"))
        except Exception:
            d["tags"] = []
        return d
    return None


async def standalone_search(
    session: AsyncSession, tenant_id: str, query: str, page: int, size: int, tags: str | None
) -> dict:
    import json
    import time

    start = time.time()
    offset = (page - 1) * size

    # Build WHERE clause
    conditions = ["tenant_id = :tenant_id", "(title LIKE :q OR content LIKE :q)"]
    params: dict = {"tenant_id": tenant_id, "q": f"%{query}%", "limit": size, "offset": offset}

    if tags:
        tag_list = [t.strip() for t in tags.split(",")]
        tag_conditions = []
        for i, tag in enumerate(tag_list):
            key = f"tag_{i}"
            tag_conditions.append(f"tags LIKE :{key}")
            params[key] = f'%"{tag}"%'
        conditions.append(f"({' OR '.join(tag_conditions)})")

    where = " AND ".join(conditions)

    # Count
    count_result = await session.execute(
        text(f"SELECT COUNT(*) FROM document_contents WHERE {where}"), params
    )
    total = count_result.scalar() or 0

    # Fetch page
    rows_result = await session.execute(
        text(f"""
            SELECT id, title, content, tags, author, file_type, created_at
            FROM document_contents WHERE {where}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )

    took_ms = int((time.time() - start) * 1000)

    results = []
    for row in rows_result.mappings().all():
        r = dict(row)
        # Simple highlight — find a snippet around the query
        content = r.get("content", "") or ""
        title = r.get("title", "")
        content_highlights = []
        idx = content.lower().find(query.lower())
        if idx >= 0:
            snippet_start = max(0, idx - 60)
            snippet_end = min(len(content), idx + len(query) + 60)
            snippet = content[snippet_start:snippet_end]
            highlighted = snippet.replace(query, f"<em>{query}</em>")
            content_highlights.append(f"...{highlighted}...")

        try:
            parsed_tags = json.loads(r.get("tags", "[]"))
        except Exception:
            parsed_tags = []

        results.append({
            "id": r["id"],
            "title": title,
            "score": 1.0,
            "highlights": {
                "title": [title.replace(query, f"<em>{query}</em>")] if query.lower() in title.lower() else [],
                "content": content_highlights,
            },
            "author": r.get("author"),
            "tags": parsed_tags,
            "created_at": r.get("created_at"),
        })

    return {
        "total_hits": total,
        "took_ms": took_ms,
        "results": results,
        "facets": {},
    }
