import logging
from typing import Any

from elasticsearch import AsyncElasticsearch

from src.config.settings import settings

logger = logging.getLogger(__name__)

es_client: AsyncElasticsearch | None = None


def get_es_client() -> AsyncElasticsearch:
    global es_client
    if es_client is None:
        es_client = AsyncElasticsearch(
            hosts=[settings.elasticsearch_url],
            request_timeout=30,
            max_retries=3,
            retry_on_timeout=True,
        )
    return es_client


async def close_es_client() -> None:
    global es_client
    if es_client:
        await es_client.close()
        es_client = None


def _index_name(tenant_id: str) -> str:
    safe = tenant_id.replace("-", "_")
    return f"docs_{safe}"


INDEX_SETTINGS = {
    "settings": {
        "number_of_shards": 3,
        "number_of_replicas": 1,
        "analysis": {
            "analyzer": {
                "doc_analyzer": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "stop", "snowball"],
                }
            }
        },
    },
    "mappings": {
        "properties": {
            "title": {"type": "text", "analyzer": "doc_analyzer", "boost": 2.0},
            "content": {"type": "text", "analyzer": "doc_analyzer"},
            "tags": {"type": "keyword"},
            "author": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "status": {"type": "keyword"},
            "file_type": {"type": "keyword"},
        }
    },
}


async def ensure_index(tenant_id: str) -> None:
    client = get_es_client()
    index = _index_name(tenant_id)
    exists = await client.indices.exists(index=index)
    if not exists:
        await client.indices.create(index=index, body=INDEX_SETTINGS)
        logger.info(f"Created ES index: {index}")


async def index_document(tenant_id: str, doc_id: str, body: dict) -> None:
    client = get_es_client()
    index = _index_name(tenant_id)
    await ensure_index(tenant_id)
    await client.index(index=index, id=doc_id, document=body)
    logger.info(f"Indexed doc {doc_id} in {index}")


async def delete_document(tenant_id: str, doc_id: str) -> None:
    client = get_es_client()
    index = _index_name(tenant_id)
    try:
        await client.delete(index=index, id=doc_id)
        logger.info(f"Deleted doc {doc_id} from {index}")
    except Exception as e:
        logger.warning(f"Failed to delete doc {doc_id} from ES: {e}")


async def get_document(tenant_id: str, doc_id: str) -> dict | None:
    client = get_es_client()
    index = _index_name(tenant_id)
    try:
        result = await client.get(index=index, id=doc_id)
        return result["_source"]
    except Exception:
        return None


async def search_documents(
    tenant_id: str,
    query: str,
    page: int = 1,
    size: int = 20,
    tags: str | None = None,
    sort: str = "relevance",
) -> dict[str, Any]:
    client = get_es_client()
    index = _index_name(tenant_id)

    # Build the query
    must = [
        {
            "multi_match": {
                "query": query,
                "fields": ["title^2", "content"],
                "fuzziness": "AUTO",
            }
        }
    ]

    filters = []
    if tags:
        tag_list = [t.strip() for t in tags.split(",")]
        filters.append({"terms": {"tags": tag_list}})

    es_query: dict[str, Any] = {
        "bool": {
            "must": must,
            "filter": filters,
        }
    }

    sort_clause = []
    if sort == "date":
        sort_clause = [{"created_at": {"order": "desc"}}, "_score"]
    else:
        sort_clause = ["_score", {"created_at": {"order": "desc"}}]

    from_offset = (page - 1) * size

    body = {
        "query": es_query,
        "from": from_offset,
        "size": size,
        "sort": sort_clause,
        "highlight": {
            "fields": {
                "title": {"number_of_fragments": 1},
                "content": {"number_of_fragments": 3, "fragment_size": 150},
            },
            "pre_tags": ["<em>"],
            "post_tags": ["</em>"],
        },
        "aggs": {
            "tags": {"terms": {"field": "tags", "size": 20}},
            "file_type": {"terms": {"field": "file_type", "size": 10}},
        },
    }

    try:
        result = await client.search(index=index, body=body)
    except Exception as e:
        logger.error(f"ES search failed for tenant {tenant_id}: {e}")
        # If the index doesn't exist yet, return empty results
        return {
            "total_hits": 0,
            "took_ms": 0,
            "results": [],
            "facets": {},
        }

    hits = result.get("hits", {})
    total = hits.get("total", {}).get("value", 0)
    took = result.get("took", 0)

    results = []
    for hit in hits.get("hits", []):
        source = hit["_source"]
        highlight = hit.get("highlight", {})
        results.append({
            "id": hit["_id"],
            "title": source.get("title", ""),
            "score": round(hit.get("_score", 0) or 0, 2),
            "highlights": {
                "title": highlight.get("title", []),
                "content": highlight.get("content", []),
            },
            "author": source.get("author"),
            "tags": source.get("tags", []),
            "created_at": source.get("created_at"),
        })

    # Parse aggregations
    facets = {}
    aggs = result.get("aggregations", {})
    for agg_name in ["tags", "file_type"]:
        if agg_name in aggs:
            facets[agg_name] = [
                {"key": bucket["key"], "count": bucket["doc_count"]}
                for bucket in aggs[agg_name].get("buckets", [])
            ]

    return {
        "total_hits": total,
        "took_ms": took,
        "results": results,
        "facets": facets,
    }


async def check_health() -> dict:
    try:
        client = get_es_client()
        info = await client.cluster.health()
        return {"status": "healthy" if info["status"] in ("green", "yellow") else "unhealthy"}
    except Exception as e:
        logger.error(f"ES health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}
