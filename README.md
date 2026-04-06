# Distributed Document Search Service

A multi-tenant document search API built with **Python/FastAPI**, **Elasticsearch**, **PostgreSQL**, **Redis**, and **RabbitMQ**. Designed for sub-second search across millions of documents with tenant isolation, caching, rate limiting, and async indexing.

## Architecture

```
Nginx/LB → FastAPI (Uvicorn) → Elasticsearch (search)
                              → PostgreSQL (metadata)
                              → Redis (cache + rate limit)
                              → RabbitMQ (async indexing)
                              → Index Worker (consumer)
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design document.

## Quick Start

### Prerequisites
- Docker & Docker Compose

### 1. Start all services

```bash
docker-compose up --build -d
```

This brings up: FastAPI app (port 8000), Index Worker, PostgreSQL, Elasticsearch, Redis, RabbitMQ.

Wait ~30 seconds for all services to be healthy, then verify:

```bash
curl http://localhost:8000/v1/health | python3 -m json.tool
```

### Alternative: Run locally without Docker (Standalone Mode)

If you don't want to deal with Docker, you can run the app directly using a virtual environment. This uses SQLite instead of PostgreSQL/ES/Redis/RabbitMQ, so no external services needed.

```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate        # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy env file and make sure standalone mode is on
cp .env.example .env

# Start the server
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

Verify it's running:

```bash
curl http://localhost:8000/v1/health | python3 -m json.tool
```

In standalone mode, ES/Redis/RabbitMQ will show as `skipped` in the health check. Search uses SQLite LIKE queries instead of Elasticsearch, and indexing happens synchronously (no worker needed).

---

### 2. Seed sample data

```bash
pip install httpx
python scripts/seed.py
```

Wait 3-5 seconds for the worker to index the documents.

### 3. Try it out

See the **Sample API Requests** section below.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/documents` | Index a new document (async) |
| `GET` | `/v1/search?q={query}` | Full-text search with facets |
| `GET` | `/v1/documents/{id}` | Get document details |
| `DELETE` | `/v1/documents/{id}` | Delete a document |
| `GET` | `/v1/health` | Health check with dependency status |

All endpoints (except health) require `X-Tenant-ID` header.

---

## Sample API Requests

### Demo Tenant IDs

```
acme-corp:   a1b2c3d4-e5f6-7890-abcd-ef1234567890
globex-inc:  b2c3d4e5-f6a7-8901-bcde-f12345678901
```

### Create a document

```bash
curl -X POST http://localhost:8000/v1/documents \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
  -d '{
    "title": "Q4 2025 Financial Report",
    "content": "Revenue increased by 23% year-over-year driven by strong enterprise sales.",
    "tags": ["finance", "quarterly-report"],
    "author": "jane.doe@acme.com",
    "file_type": "pdf",
    "metadata": {"department": "finance"}
  }'
```

### Search documents

```bash
# Basic search
curl "http://localhost:8000/v1/search?q=financial+report" \
  -H "X-Tenant-ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890"

# With tag filter
curl "http://localhost:8000/v1/search?q=financial+report&tags=finance" \
  -H "X-Tenant-ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890"

# Paginated
curl "http://localhost:8000/v1/search?q=engineering&page=1&size=5" \
  -H "X-Tenant-ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890"

# Search a different tenant (tenant isolation demo)
curl "http://localhost:8000/v1/search?q=product+roadmap" \
  -H "X-Tenant-ID: b2c3d4e5-f6a7-8901-bcde-f12345678901"
```

### Get document details

```bash
curl http://localhost:8000/v1/documents/{document_id} \
  -H "X-Tenant-ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890"
```

### Delete a document

```bash
curl -X DELETE http://localhost:8000/v1/documents/{document_id} \
  -H "X-Tenant-ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890"
```

### Health check

```bash
curl http://localhost:8000/v1/health | python3 -m json.tool
```

---

## Project Structure

```
Document-Search/
├── docker-compose.yml          # All services orchestration
├── Dockerfile                  # App + worker image
├── requirements.txt            # Python dependencies
├── .env.example                # Environment variables
├── docs/
│   └── ARCHITECTURE.md         # Architecture design document
├── scripts/
│   ├── init_db.sql             # PostgreSQL schema + seed tenants
│   └── seed.py                 # Populate sample documents via API
└── src/
    ├── main.py                 # FastAPI app entry point
    ├── worker.py               # RabbitMQ consumer (index worker)
    ├── config/
    │   └── settings.py         # Pydantic settings (env-based config)
    ├── models/
    │   └── schemas.py          # Request/response Pydantic models
    ├── routes/
    │   ├── documents.py        # POST/GET/DELETE /v1/documents
    │   ├── search.py           # GET /v1/search
    │   └── health.py           # GET /v1/health
    ├── middleware/
    │   ├── tenant.py           # Tenant resolution (FastAPI Depends)
    │   ├── rate_limiter.py     # Per-tenant rate limiting (Redis)
    │   └── error_handler.py    # Consistent error responses
    └── services/
        ├── database.py         # PostgreSQL async operations
        ├── elasticsearch.py    # ES indexing, search, health
        ├── cache.py            # L1 (in-memory) + L2 (Redis) cache
        └── queue.py            # RabbitMQ producer + topology setup
```

## Key Design Decisions

- **Async indexing via RabbitMQ** — POST returns 202 immediately; heavy ES indexing happens in background worker
- **Index-per-tenant** — Each tenant gets a dedicated ES index for strong data isolation
- **Two-tier caching** — In-memory LRU (5s) + Redis (60s) in front of Elasticsearch
- **Sliding window rate limiting** — Redis sorted sets, per-tenant limits
- **UUIDv7** — Time-sortable document IDs with good PG B-tree locality

## Swagger Docs

Once running, visit: http://localhost:8000/docs