# Architecture Design Document — Distributed Document Search Service

**Version:** 1.0
**Date:** April 2026

---

## 1. High-Level System Architecture

```
                                    ┌─────────────────────────────────┐
                                    │         API Gateway /           │
                                    │       Load Balancer (Nginx)     │
                                    └──────────────┬──────────────────┘
                                                   │
                              ┌────────────────────┼────────────────────┐
                              │                    │                    │
                     ┌────────▼───────┐  ┌────────▼───────┐  ┌────────▼───────┐
                     │  App Server 1  │  │  App Server 2  │  │  App Server N  │
                     │  (Python /     │  │  (Python /     │  │  (Python /     │
                     │   FastAPI)     │  │   FastAPI)     │  │   FastAPI)     │
                     └──┬──┬──┬──┬───┘  └──┬──┬──┬──┬───┘  └──┬──┬──┬──┬───┘
                        │  │  │  │         │  │  │  │         │  │  │  │
           ┌────────────┘  │  │  └──────┐  │  │  │  │  ┌─────┘  │  │  └─────┐
           │               │  │         │  │  │  │  │  │        │  │        │
  ┌────────▼────────┐ ┌────▼──▼───┐ ┌───▼──▼──▼──▼──▼──▼───┐ ┌─▼──▼────────▼─┐
  │    Redis        │ │ RabbitMQ  │ │   Elasticsearch       │ │  PostgreSQL   │
  │  (Cache +       │ │ (Async    │ │   Cluster             │ │  (Metadata +  │
  │  Rate Limiting) │ │  Indexing │ │  (Search Engine)      │ │  Tenant Cfg)  │
  │                 │ │  Queue)   │ │                       │ │               │
  └─────────────────┘ └─────┬─────┘ └───────────────────────┘ └───────────────┘
                            │
                   ┌────────▼────────┐
                   │  Index Worker   │
                   │  (Consumer)     │
                   │  Processes docs │
                   │  from queue →   │
                   │  writes to ES   │
                   │  + PostgreSQL   │
                   └─────────────────┘
```

### Component Responsibilities

| Component | Role |
|-----------|------|
| **API Gateway / Load Balancer** | TLS termination, request routing, basic DDoS protection |
| **App Servers (Python/FastAPI)** | Stateless async REST API — handles search queries, document CRUD, tenant resolution, rate limiting. Uvicorn ASGI server with multiple workers for concurrency. |
| **Elasticsearch Cluster** | Primary search engine — full-text indexing, relevance scoring, fuzzy/faceted search, highlighting |
| **PostgreSQL** | System of record — document metadata, tenant configuration, audit logs |
| **Redis** | L2 query cache (shared across app instances), per-tenant rate limit counters, distributed locks |
| **RabbitMQ** | Decouples write path — buffers document index/delete operations for async processing |
| **Index Worker** | Consumes messages from RabbitMQ, writes to Elasticsearch and PostgreSQL, handles retries |

---

## 2. Data Flow Diagrams

### 2.1 Document Indexing (Write Path)

```
Client                App Server            RabbitMQ         Index Worker       Elasticsearch    PostgreSQL
  │                       │                    │                  │                  │               │
  │  POST /documents      │                    │                  │                  │               │
  │  X-Tenant-ID: acme    │                    │                  │                  │               │
  │──────────────────────►│                    │                  │                  │               │
  │                       │                    │                  │                  │               │
  │                       │ 1. Validate payload│                  │                  │               │
  │                       │    + tenant auth   │                  │                  │               │
  │                       │                    │                  │                  │               │
  │                       │ 2. Generate doc ID │                  │                  │               │
  │                       │    (UUIDv7)        │                  │                  │               │
  │                       │                    │                  │                  │               │
  │                       │ 3. Publish message │                  │                  │               │
  │                       │───────────────────►│                  │                  │               │
  │                       │                    │                  │                  │               │
  │  202 Accepted         │                    │                  │                  │               │
  │  { id, status:        │                    │                  │                  │               │
  │    "processing" }     │                    │                  │                  │               │
  │◄──────────────────────│                    │                  │                  │               │
  │                       │                    │ 4. Consume msg   │                  │               │
  │                       │                    │─────────────────►│                  │               │
  │                       │                    │                  │                  │               │
  │                       │                    │                  │ 5. Index document│               │
  │                       │                    │                  │─────────────────►│               │
  │                       │                    │                  │                  │               │
  │                       │                    │                  │ 6. Store metadata│               │
  │                       │                    │                  │──────────────────────────────────►
  │                       │                    │                  │                  │               │
  │                       │                    │                  │ 7. ACK message   │               │
  │                       │                    │◄─────────────────│                  │               │
```

**Why this flow works well:**
- We return `202 Accepted` immediately — the client doesn't sit around waiting for ES to tokenize and analyze the document. That heavy lifting happens in the background, which also means traffic spikes don't hammer ES directly.
- UUIDv7 for document IDs. They're time-sortable (useful for "newest first" queries) and globally unique without needing a central sequence generator.
- The worker ACKs the message only after both ES and PG writes succeed. If something fails mid-way, the message gets redelivered. ES upserts by `_id` are idempotent, so duplicate processing is harmless.

### 2.2 Search (Read Path)

```
Client              App Server              Redis               Elasticsearch
  │                     │                     │                      │
  │ GET /search?q=..    │                     │                      │
  │ &tenant=acme        │                     │                      │
  │────────────────────►│                     │                      │
  │                     │                     │                      │
  │                     │ 1. Resolve tenant   │                      │
  │                     │ 2. Check rate limit │                      │
  │                     │────────────────────►│                      │
  │                     │◄────────────────────│                      │
  │                     │                     │                      │
  │                     │ 3. Check cache      │                      │
  │                     │────────────────────►│                      │
  │                     │    MISS             │                      │
  │                     │◄────────────────────│                      │
  │                     │                     │                      │
  │                     │ 4. Query ES (tenant │                      │
  │                     │    scoped index)    │                      │
  │                     │────────────────────────────────────────────►
  │                     │                     │                      │
  │                     │ 5. Results          │                      │
  │                     │◄────────────────────────────────────────────
  │                     │                     │                      │
  │                     │ 6. Cache results    │                      │
  │                     │    (TTL: 60s)       │                      │
  │                     │────────────────────►│                      │
  │                     │                     │                      │
  │ 200 OK { results }  │                     │                      │
  │◄────────────────────│                     │                      │
```

**How the read path stays fast:**
- Every search query goes against a tenant-specific index (`docs_{tenantId}`). There's no filter clause to forget — you physically can't see another tenant's data.
- Two cache layers sit in front of ES. The in-memory LRU (5s TTL) catches rapid-fire identical queries from the same instance. Redis (60s TTL) handles the cross-instance case. Most read traffic never touches ES at all.
- Cache keys are built as `search:{tenantId}:{sha256(query+filters+page)}` — predictable, no collisions, easy to invalidate by tenant prefix.

---

## 3. Database / Storage Strategy

### Why Elasticsearch as the Primary Search Engine

| Requirement | Elasticsearch Fit |
|-------------|-------------------|
| 10M+ documents, sub-500ms p95 | Distributed by design, inverted index, shard-level parallelism |
| Full-text search + relevance | BM25 scoring, configurable analyzers, synonyms, stemming |
| Fuzzy search, facets, highlighting | Native support — no application-layer workaround |
| Horizontal scaling | Add shards/nodes, index-per-tenant allows independent scaling |
| 1000+ concurrent searches/sec | Connection pooling, replica shards serve reads in parallel |

**Why not PostgreSQL FTS alone?** I've used PG full-text search on smaller projects and it's solid up to maybe a million documents. Past that point, query latency starts creeping up, relevance tuning options are limited compared to ES, and scaling reads horizontally means setting up streaming replication — a lot of operational complexity for something ES handles out of the box.

### Storage Layer Breakdown

| Store | Data | Justification |
|-------|------|---------------|
| **Elasticsearch** | Full document content + search index | Optimized for full-text queries, relevance, aggregations |
| **PostgreSQL** | Document metadata (id, tenant, title, created_at, status, size, content_hash), tenant configuration, API keys, rate limit configs | ACID transactions, relational integrity, the authoritative record |
| **Redis** | Query cache, rate limit counters, circuit breaker state | Sub-ms reads, TTL-based expiry, atomic counters |
| **RabbitMQ** | Indexing job queue (document create/update/delete events) | Durable queues, message acknowledgement, dead-letter exchange for failures |

### Elasticsearch Index Design

```json
// Index per tenant: docs_acme, docs_globex, etc.
{
  "settings": {
    "number_of_shards": 3,
    "number_of_replicas": 1,
    "analysis": {
      "analyzer": {
        "doc_analyzer": {
          "type": "custom",
          "tokenizer": "standard",
          "filter": ["lowercase", "stop", "snowball"]
        }
      }
    }
  },
  "mappings": {
    "properties": {
      "title":      { "type": "text", "analyzer": "doc_analyzer", "boost": 2.0 },
      "content":    { "type": "text", "analyzer": "doc_analyzer" },
      "tags":       { "type": "keyword" },
      "author":     { "type": "keyword" },
      "tenant_id":  { "type": "keyword" },
      "created_at": { "type": "date" },
      "updated_at": { "type": "date" },
      "status":     { "type": "keyword" },
      "file_type":  { "type": "keyword" }
    }
  }
}
```

**Why this mapping looks the way it does:**
- `title` gets a 2x boost because if someone searches "financial report" and we have a doc literally titled "Financial Report", that should rank above a doc that just mentions it in paragraph 14.
- Tags, author, status, file_type are all `keyword` — we don't need full-text analysis on them, just exact match filters and faceted counts.
- The custom analyzer chains `lowercase → stop → snowball`. The snowball stemmer means a search for "running" also picks up "run", "runs", etc. Good enough for most English-language document corpora.
- Starting with 3 primary shards per tenant index. This can be dialed up for large tenants or down to 1 for small ones — the index-per-tenant model gives us that flexibility.

### PostgreSQL Schema

```sql
CREATE TABLE tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL UNIQUE,
    api_key     VARCHAR(512) NOT NULL,
    rate_limit  INTEGER DEFAULT 100,     -- requests per second
    is_active   BOOLEAN DEFAULT true,
    config      JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE documents (
    id            UUID PRIMARY KEY,
    tenant_id     UUID NOT NULL REFERENCES tenants(id),
    title         VARCHAR(1024) NOT NULL,
    content_hash  VARCHAR(64),           -- SHA-256, for dedup
    file_type     VARCHAR(50),
    status        VARCHAR(20) DEFAULT 'processing',  -- processing | indexed | failed
    metadata      JSONB DEFAULT '{}',
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_documents_tenant    ON documents(tenant_id);
CREATE INDEX idx_documents_status    ON documents(tenant_id, status);
CREATE INDEX idx_documents_created   ON documents(tenant_id, created_at DESC);
```

---

## 4. API Design

### Base URL & Headers

```
Base URL:  https://api.docsearch.example.com/v1
Headers:
  X-Tenant-ID: <tenant_id>       (required — identifies tenant)
  Authorization: Bearer <token>   (required — API key or JWT)
  X-Request-ID: <uuid>           (optional — for tracing)
```

### Endpoints & Contracts

#### POST /v1/documents — Index a New Document

```http
POST /v1/documents
Content-Type: application/json
X-Tenant-ID: acme-corp
Authorization: Bearer sk_live_xxx

{
  "title": "Q4 2025 Financial Report",
  "content": "Revenue increased by 23% year-over-year...",
  "tags": ["finance", "quarterly-report"],
  "author": "jane.doe@acme.com",
  "file_type": "pdf",
  "metadata": {
    "department": "finance",
    "confidentiality": "internal"
  }
}
```

**Response: 202 Accepted**
```json
{
  "id": "01906b3a-4f7a-7def-8c1a-2b3c4d5e6f7a",
  "status": "processing",
  "message": "Document queued for indexing",
  "created_at": "2026-04-05T06:30:00.000Z"
}
```

#### GET /v1/search — Search Documents

```http
GET /v1/search?q=financial+report&page=1&size=20&tags=finance&sort=relevance
X-Tenant-ID: acme-corp
Authorization: Bearer sk_live_xxx
```

**Response: 200 OK**
```json
{
  "query": "financial report",
  "total_hits": 142,
  "page": 1,
  "size": 20,
  "took_ms": 34,
  "results": [
    {
      "id": "01906b3a-4f7a-7def-8c1a-2b3c4d5e6f7a",
      "title": "Q4 2025 Financial Report",
      "score": 12.45,
      "highlights": {
        "title": ["Q4 2025 <em>Financial Report</em>"],
        "content": ["Revenue increased by 23%... <em>financial</em> targets exceeded..."]
      },
      "author": "jane.doe@acme.com",
      "tags": ["finance", "quarterly-report"],
      "created_at": "2026-04-05T06:30:00.000Z"
    }
  ],
  "facets": {
    "tags": [
      { "key": "finance", "count": 89 },
      { "key": "quarterly-report", "count": 42 }
    ],
    "file_type": [
      { "key": "pdf", "count": 98 },
      { "key": "docx", "count": 44 }
    ]
  }
}
```

#### GET /v1/documents/:id — Retrieve Document Details

```http
GET /v1/documents/01906b3a-4f7a-7def-8c1a-2b3c4d5e6f7a
X-Tenant-ID: acme-corp
Authorization: Bearer sk_live_xxx
```

**Response: 200 OK**
```json
{
  "id": "01906b3a-4f7a-7def-8c1a-2b3c4d5e6f7a",
  "title": "Q4 2025 Financial Report",
  "content": "Revenue increased by 23% year-over-year...",
  "tags": ["finance", "quarterly-report"],
  "author": "jane.doe@acme.com",
  "file_type": "pdf",
  "status": "indexed",
  "metadata": {
    "department": "finance",
    "confidentiality": "internal"
  },
  "created_at": "2026-04-05T06:30:00.000Z",
  "updated_at": "2026-04-05T06:30:02.340Z"
}
```

#### DELETE /v1/documents/:id — Remove a Document

```http
DELETE /v1/documents/01906b3a-4f7a-7def-8c1a-2b3c4d5e6f7a
X-Tenant-ID: acme-corp
Authorization: Bearer sk_live_xxx
```

**Response: 200 OK**
```json
{
  "id": "01906b3a-4f7a-7def-8c1a-2b3c4d5e6f7a",
  "status": "deleted",
  "message": "Document deletion queued"
}
```

#### GET /v1/health — Health Check

```http
GET /v1/health
```

**Response: 200 OK**
```json
{
  "status": "healthy",
  "uptime_seconds": 84320,
  "timestamp": "2026-04-05T06:35:00.000Z",
  "dependencies": {
    "elasticsearch": { "status": "healthy", "latency_ms": 4 },
    "postgresql":    { "status": "healthy", "latency_ms": 2 },
    "redis":         { "status": "healthy", "latency_ms": 1 },
    "rabbitmq":      { "status": "healthy", "latency_ms": 3 }
  }
}
```

### Error Response Contract

Every error response uses the same envelope, so clients can write one error handler:

```json
{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Tenant 'acme-corp' has exceeded 100 requests/second",
    "request_id": "req_abc123",
    "timestamp": "2026-04-05T06:35:00.000Z"
  }
}
```

| HTTP Status | Code | Scenario |
|-------------|------|----------|
| 400 | `VALIDATION_ERROR` | Malformed request body |
| 401 | `UNAUTHORIZED` | Missing or invalid API key |
| 403 | `FORBIDDEN` | Tenant accessing another tenant's resource |
| 404 | `NOT_FOUND` | Document does not exist |
| 429 | `RATE_LIMIT_EXCEEDED` | Per-tenant rate limit hit |
| 503 | `SERVICE_UNAVAILABLE` | Dependency down, circuit breaker open |

---

## 5. Consistency Model & Trade-offs

The short version: metadata is strongly consistent (PostgreSQL), search is eventually consistent (Elasticsearch + cache). Here's the breakdown:

| Operation | Consistency | Rationale |
|-----------|-------------|-----------|
| **Document write → PostgreSQL** | **Strong** (ACID) | PG is the system of record. After POST returns 202, the metadata row exists. |
| **Document write → Elasticsearch** | **Eventual** (~1-3s) | ES `refresh_interval` is 1s by default. Acceptable: users don't expect a just-uploaded doc to appear in search instantly. |
| **Search results** | **Eventual** | Served from ES (or cache). Cache TTL adds up to 60s of staleness. |
| **Document delete** | **Eventual** | Queued via RabbitMQ. ES index removal is near-instant once processed. |

### Trade-off: AP over CP (CAP theorem context)

We're deliberately choosing availability over strict consistency. If there's a network partition:
- Search keeps working off whichever ES replicas are reachable. Results might be a few seconds stale, but the service stays up.
- Writes pile up in RabbitMQ and get drained once things recover. Nothing is lost.
- From a product standpoint, users can tolerate slightly stale search results for a few seconds — but they won't tolerate the search being down. That trade-off drives the entire design here.

---

## 6. Caching Strategy

### Three-Tier Caching Architecture

```
Request → [L1: In-Memory LRU] → [L2: Redis] → [L3: Elasticsearch]
              5s TTL                60s TTL         Source of truth
              Per-instance          Shared           for search
              ~10K entries          ~500K entries
```

| Layer | Technology | TTL | Scope | Use Case |
|-------|-----------|-----|-------|----------|
| **L1** | Python `cachetools` TTLCache | 5 seconds | Per app instance | Absorb repeated identical queries within a short burst |
| **L2** | Redis | 60 seconds | Shared across all instances | Cross-instance cache, reduces ES load significantly |
| **L3** | Elasticsearch | — | Persistent | Authoritative search index |

### Cache Invalidation

- **On document write/delete:** We blow away all cached search results for that tenant. Practically, this means scanning Redis keys matching `search:{tenantId}:*` with `SCAN` + `UNLINK` (never `KEYS` — that blocks the Redis event loop on large keyspaces).
- **TTL does most of the work.** Explicit invalidation handles writes/deletes, but the 60s TTL is the safety net. Keeps the invalidation logic from getting too clever.
- **No cache for single-document fetches.** A PG lookup by primary key is already <5ms. Caching it would just mean more invalidation headaches for negligible gain.

### What Gets Cached

| Cached | Not Cached |
|--------|------------|
| Search query results | Document detail (PG PK lookup is <5ms) |
| Rate limit counters (Redis) | Health check responses |
| Tenant config (L1, 30s TTL) | Write operations |

---

## 7. Message Queue — Asynchronous Operations

### RabbitMQ Topology

```
                    ┌──────────────────────┐
                    │   Exchange:          │
  App Server ──────►│   doc.events         │
  (Producer)        │   (topic exchange)   │
                    └──────┬───────┬───────┘
                           │       │
              routing key: │       │ routing key:
             doc.index     │       │ doc.delete
                           │       │
                    ┌──────▼──┐ ┌──▼──────┐
                    │ Queue:  │ │ Queue:  │
                    │ indexing│ │ deletion│
                    └────┬────┘ └────┬────┘
                         │           │
                    ┌────▼───────────▼────┐
                    │   Index Worker      │
                    │   (Consumer)        │
                    └────────┬────────────┘
                             │
                    On failure (3 retries):
                             │
                    ┌────────▼────────────┐
                    │  Dead Letter Queue  │
                    │  (for inspection    │
                    │   and manual retry) │
                    └─────────────────────┘
```

### Why Async Indexing?

We've been down the path of writing directly to Elasticsearch from our API, and it's a recipe for disaster. You end up with a tight coupling between your API's response time and Elasticsearch's indexing speed. Here's how that plays out:

| Concern | Sync (write in request) | Async (via queue) |
|---------|------------------------|-------------------|
| **API latency** | Your API is blocked for 50-200ms waiting on Elasticsearch | Returns 202 in ~5ms, no waiting around |
| **Traffic spikes** | Elasticsearch gets slammed, starts rejecting requests | Queue absorbs the burst, workers process at a steady rate |
| **When Elasticsearch is slow or down** | Client gets a 500, has to retry (not ideal) | Message sits in the queue, gets processed when Elasticsearch recovers |
| **Scaling** | API and indexing throughput are coupled (hard to scale) | Add more workers without touching the API layer (easy scaling) |

### Message Schema

```json
{
  "event_type": "document.index",
  "tenant_id": "acme-corp",
  "document_id": "01906b3a-4f7a-7def-8c1a-2b3c4d5e6f7a",
  "payload": {
    "title": "Q4 2025 Financial Report",
    "content": "...",
    "tags": ["finance"],
    "author": "jane.doe@acme.com",
    "file_type": "pdf"
  },
  "metadata": {
    "published_at": "2026-04-05T06:30:00.000Z",
    "retry_count": 0,
    "request_id": "req_abc123"
  }
}
```

---

## 8. Multi-Tenancy Approach & Data Isolation

### Strategy: Index-per-Tenant

```
Tenant: acme-corp    →  ES Index: docs_acme_corp
Tenant: globex-inc   →  ES Index: docs_globex_inc
Tenant: initech      →  ES Index: docs_initech
```

### Why Index-per-Tenant (vs. Shared Index with Tenant Field)

| Dimension | Index-per-Tenant ✅ | Shared Index ❌ |
|-----------|---------------------|-----------------|
| **Data isolation** | Complete physical isolation | Logical only — a query bug could leak data |
| **Per-tenant tuning** | Independent shard count, analyzers, replicas | One-size-fits-all |
| **Noisy neighbor** | Heavy tenant won't slow others | One tenant's large query degrades all |
| **Deletion/compliance** | Drop entire index = instant GDPR purge | Expensive delete-by-query |
| **Scaling** | Move hot tenants to dedicated nodes | Complex shard allocation rules |
| **Downside** | More ES indices to manage (solvable with ILM) | — |

### Isolation Layers

Tenant isolation isn't a single gate — it's enforced at every layer so that one missed check can't cause a data leak:

| Layer | How it's enforced |
|-------|-------------------|
| **API** | `X-Tenant-ID` header is validated against the tenants table on every single request. No valid tenant = 401. |
| **Application** | A FastAPI `Depends()` function resolves the tenant and attaches it to the request. Every service call downstream receives it — there's no way to accidentally make an unscoped query. |
| **Elasticsearch** | Queries go to `docs_{tenantId}` index. Not a filter on a shared index — a physically separate index. You'd have to explicitly construct the wrong index name to cross boundaries. |
| **PostgreSQL** | Every query includes `WHERE tenant_id = ...`. The FK constraint ensures the tenant actually exists. |
| **Cache** | Keys are prefixed with the tenant ID. Even if you could guess a cache key, it's scoped. |
| **Queue** | Every message carries `tenant_id`. The worker validates it before processing — a message for a deactivated tenant gets dropped to the DLQ. |

### Tenant Lifecycle

```
Tenant Onboarding:
  1. Insert row in tenants table → generates API key
  2. Create ES index docs_{tenant_id} with configured settings
  3. Set up rate limit config in Redis

Tenant Offboarding:
  1. Set tenant.is_active = false (soft delete)
  2. Drop ES index docs_{tenant_id}
  3. Purge Redis cache keys for tenant
  4. Archive PG data per retention policy
```

---

## Architecture Decision Records (Summary)

| Decision | Choice | Alternatives Considered | Rationale |
|----------|--------|------------------------|-----------|
| Application framework | Python / FastAPI + Uvicorn | Django, Flask, Node.js/Express, Go/Gin | We need async I/O everywhere (ES, Redis, PG, RabbitMQ are all network calls). FastAPI does this natively with async/await, gives us Pydantic validation and auto-generated Swagger docs for free. Django pulls in a full ORM and admin panel we'd never use. Flask can do async but it's bolted on. Uvicorn as the ASGI server keeps things lightweight. |
| Search engine | Elasticsearch | PostgreSQL FTS, Apache Solr, Meilisearch | At 10M+ docs with sub-500ms p95, ES is the proven choice. Solr could work but the ecosystem and client libraries have shifted toward ES. Meilisearch is great for smaller use cases but less battle-tested at this scale. |
| Metadata store | PostgreSQL | MongoDB, DynamoDB | Tenant config and document metadata are inherently relational (tenant has many documents). We need ACID for things like tenant onboarding. PG is the obvious fit — no reason to reach for a NoSQL store here. |
| Cache | Redis | Memcached, Hazelcast | We're using Redis for more than just key-value caching — sorted sets for rate limit sliding windows, Lua scripts for atomic operations, pub/sub for cache invalidation signals. Memcached can't do any of that. |
| Queue | RabbitMQ | Kafka, Redis Streams, SQS | RabbitMQ gives us durable delivery, dead-letter queues, and routing out of the box. Kafka would be the pick if we needed event sourcing or replay, but that's not the case here — it'd just add operational overhead for no gain. |
| Multi-tenancy | Index-per-tenant | Shared index, schema-per-tenant | Discussed in detail in Section 8. The short version: physical isolation beats logical isolation when you have compliance requirements (GDPR right-to-deletion = drop an index) and noisy-neighbor concerns. |
| IDs | UUIDv7 | UUIDv4, auto-increment, ULID | UUIDv4 is random — poor index locality in PG B-trees. Auto-increment needs a single sequence (coordination bottleneck). UUIDv7 gives us time-ordering, uniqueness, and good index performance in one shot. |
| Consistency | Eventual (search), Strong (metadata) | Full strong consistency | Making search strongly consistent would mean either synchronous ES writes (slow) or read-after-write guarantees (complex). The business doesn't need it — a 1-3 second delay before a new doc appears in search is perfectly fine. Metadata ("does this document exist?") needs to be correct immediately. |
