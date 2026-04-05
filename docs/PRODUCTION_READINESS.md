# Production Readiness Analysis — Distributed Document Search Service

**Version:** 1.0
**Date:** April 2026

---

## 1. Scalability — Handling 100x Growth

### Current Baseline

The prototype handles a single-node Elasticsearch cluster, one app server, and one worker process. That's fine for development. Getting to 100x (1B+ documents, 100K+ searches/sec) requires deliberate changes at each layer.

### Elasticsearch Scaling

**Index-per-tenant already gives us the right foundation.** Each tenant's index can be sized independently — a 50M-document tenant gets more primary shards than one with 10K docs.

Concrete steps for 100x:

- **Shard sizing:** Keep each shard between 10-50 GB. A tenant with 200M documents might need 20+ primary shards instead of the default 3. We'd automate this in the tenant onboarding flow based on estimated document volume.
- **Dedicated node roles:** Separate master-eligible, data, and coordinating nodes. Coordinating nodes handle search fan-out without competing for heap with indexing.
- **Hot-warm-cold architecture:** Recent documents (last 30 days) live on SSD-backed hot nodes. Older data rolls to warm/cold tiers via Index Lifecycle Management (ILM). Cuts storage costs significantly — most search traffic hits recent data anyway.
- **Read replicas:** Bump replica count from 1 to 2-3 for high-traffic tenants. ES distributes search load across replicas automatically.
- **Cross-cluster search (CCS):** If we outgrow a single cluster, we can shard by tenant groups across clusters and use CCS to unify search. This is a last resort — a well-tuned single cluster can handle a lot before we get here.

### Application Layer

- **Horizontal scaling is already built in.** The FastAPI app servers are stateless — session state lives in Redis, tenant context comes from headers. Spin up more instances behind the load balancer and traffic distributes.
- **Worker scaling:** Add more worker instances consuming from the same RabbitMQ queues. RabbitMQ round-robins messages across consumers, so throughput scales linearly with worker count.
- **Auto-scaling:** In production (Kubernetes or ECS), we'd set HPA policies keyed on request latency p95 and CPU utilization for app servers, and queue depth for workers.

### Database Scaling

- **PostgreSQL read replicas:** Search metadata reads (document detail lookups) can go to replicas. Writes stay on the primary. Connection pooling via PgBouncer reduces connection overhead.
- **Partitioning:** If the documents table grows past ~500M rows, partition by `tenant_id` using PostgreSQL declarative partitioning. This keeps index scans fast and makes tenant offboarding (drop a partition) nearly instant.

### Caching at Scale

- **Redis Cluster:** Move from single-node Redis to a cluster (6+ nodes with hash slots). This gives us both more memory and more throughput.
- **Cache hit ratio monitoring:** At 100x scale, the difference between 80% and 95% cache hit ratio is enormous. We'd track this metric and tune TTLs per-tenant based on their query patterns.

---

## 2. Resilience — Failure is Expected

### Circuit Breakers

Every external dependency gets a circuit breaker. The pattern: if a dependency fails N times in a window, stop calling it for a cooldown period. This prevents cascade failures where one slow service takes down everything.

| Dependency | Breaker Config | Fallback Behavior |
|------------|---------------|-------------------|
| **Elasticsearch** | Open after 5 failures in 30s, half-open after 15s | Return cached results if available, otherwise 503 with clear error message |
| **Redis** | Open after 10 failures in 30s, half-open after 10s | Skip cache (L1 still works), disable rate limiting (allow traffic through) |
| **RabbitMQ** | Open after 5 failures in 30s, half-open after 15s | Write to a local fallback queue (SQLite or filesystem), drain to RabbitMQ when it recovers |
| **PostgreSQL** | Open after 3 failures in 30s, half-open after 20s | Hard dependency — 503 if PG is down. No safe fallback for the system of record. |

Implementation: Python's `tenacity` library (already in our requirements) handles retries. For circuit breaking, we'd use `pybreaker` or build a lightweight version on top of Redis (shared state across instances).

### Retry Strategy

Not all failures deserve the same retry behavior:

- **Transient errors (connection timeout, 502/503):** Exponential backoff with jitter. Start at 100ms, cap at 5s, max 3 attempts.
- **Rate limit errors (429):** Respect `Retry-After` header. No jitter needed — the server told us when to come back.
- **Client errors (400, 401, 404):** No retry. These won't succeed on the second attempt.
- **Worker message processing:** 3 retries with exponential backoff. After that, the message goes to the dead letter queue for manual inspection. We never silently drop messages.

### Failover

- **Elasticsearch:** With replica shards, ES handles node failure automatically. If a primary shard's node goes down, its replica gets promoted. Zero application-level intervention needed.
- **PostgreSQL:** Streaming replication with automatic failover via Patroni or AWS RDS Multi-AZ. The connection string points to a virtual IP or DNS entry that follows the primary.
- **Redis:** Redis Sentinel (or ElastiCache Multi-AZ) for automatic failover. Our app already treats Redis as optional — if it's down, we degrade gracefully.
- **RabbitMQ:** Quorum queues (replacing classic mirrored queues) for cross-node replication. Messages survive a node failure.

### Graceful Degradation Hierarchy

When things break, we degrade in stages rather than going fully offline:

```
Level 0: Everything healthy → Full functionality
Level 1: Redis down → No L2 cache, no rate limiting, search still works (just slower)
Level 2: RabbitMQ down → Sync indexing fallback, search unaffected
Level 3: ES degraded → Serve from cache, stale results better than no results
Level 4: ES down → Search returns 503, document CRUD still works via PG
Level 5: PG down → Full outage, nothing we can safely do
```

---

## 3. Security

### Authentication & Authorization

**Current state:** API key passed via `X-Tenant-ID` header with tenant lookup. This is fine for a prototype.

**Production approach:**

- **API keys for server-to-server:** Issue scoped API keys per tenant, hashed with bcrypt in the database. Keys support rotation — tenants can have multiple active keys during a transition period.
- **JWT for user-facing access:** OAuth 2.0 / OpenID Connect for user authentication. JWTs carry tenant_id and permission claims. Validated locally (no auth server round-trip on every request) using RS256 with key rotation via JWKS endpoint.
- **RBAC within tenants:** Not every user in a tenant should be able to delete documents. Define roles (reader, editor, admin) and enforce at the API layer.

```
Authorization flow:
  Request → API Gateway (TLS termination)
          → Rate limit check (Redis)
          → JWT validation (local, RS256)
          → Tenant resolution (from JWT claims)
          → Permission check (role-based)
          → Route handler
```

### Encryption

| Layer | Mechanism |
|-------|-----------|
| **In transit** | TLS 1.3 everywhere. API Gateway terminates external TLS. Internal service-to-service uses mTLS (mutual TLS) via service mesh (Istio/Linkerd) or certificate-based auth. |
| **At rest — PostgreSQL** | Transparent Data Encryption (TDE) or volume-level encryption (AWS EBS encryption, GCP CMEK). |
| **At rest — Elasticsearch** | Encrypted filesystem or Elastic's native encryption at rest (requires Platinum license, otherwise volume encryption). |
| **At rest — Redis** | In-memory by nature. If using Redis persistence (RDB/AOF), encrypt the underlying volume. |
| **Secrets** | HashiCorp Vault or AWS Secrets Manager for API keys, DB passwords, certificates. Never in environment variables in production. |

### API Security

- **Input validation:** Pydantic already validates request bodies. Add max content length (10MB default, configurable per tenant), content type validation, and sanitize search queries to prevent Elasticsearch injection.
- **Rate limiting:** Already implemented per-tenant. Production would add per-IP rate limiting at the API gateway level to catch abuse before it hits our app.
- **CORS:** Strict allow-list for browser-based clients. No wildcard origins.
- **Request size limits:** Nginx `client_max_body_size` plus application-level validation.
- **Audit logging:** Every write operation (create, update, delete) gets an audit log entry: who did it, when, from what IP, what changed. Immutable append-only table, separate from operational data.

### Tenant Data Isolation Verification

Beyond the runtime isolation described in the architecture doc, we'd add:
- **Automated penetration testing:** Regularly attempt cross-tenant access via API. If tenant A can see tenant B's documents, the test fails the build.
- **Query logging with tenant context:** Every ES and PG query includes the tenant ID. A background job scans query logs for any query missing a tenant scope — that's a bug.

---

## 4. Observability

### Metrics (Prometheus + Grafana)

You can't fix what you can't see. These are the key metrics, grouped by what question they answer:

**Is the system healthy?**
- Request rate, error rate, and latency (p50, p95, p99) per endpoint
- Dependency health (ES, PG, Redis, RabbitMQ) — up/down and latency
- Active connections per dependency

**Is search performing well?**
- Search latency breakdown: cache check time, ES query time, serialization time
- Cache hit ratio (L1 and L2 separately)
- ES query rate and indexing rate per tenant
- Queue depth (indexing backlog)

**Are we running out of room?**
- ES disk usage per index (tenant), shard sizes
- PG table sizes, dead tuple count (vacuum health)
- Redis memory usage, eviction count
- RabbitMQ queue depth, consumer count, message age

**Per-tenant visibility:**
- Request rate per tenant (catch noisy neighbors early)
- Document count per tenant
- Search latency per tenant (some tenants' data characteristics may cause slower queries)

### Logging (ELK or Loki)

Structured JSON logging with consistent fields:

```json
{
  "timestamp": "2026-04-05T06:30:00.000Z",
  "level": "INFO",
  "service": "api",
  "trace_id": "abc123",
  "span_id": "def456",
  "tenant_id": "acme-corp",
  "method": "GET",
  "path": "/v1/search",
  "status": 200,
  "latency_ms": 34,
  "cache_hit": true,
  "msg": "Search completed"
}
```

Every log line carries `trace_id` and `tenant_id`. This makes it trivial to filter by tenant during incident investigation or trace a single request across services.

Log levels in production:
- **INFO:** Request/response summaries, startup/shutdown events
- **WARN:** Cache misses, retry attempts, rate limit near-misses
- **ERROR:** Failed dependency calls, unhandled exceptions, data inconsistencies

We do NOT log document content (PII risk) or full request bodies (too verbose).

### Distributed Tracing (OpenTelemetry + Jaeger/Tempo)

With multiple services (API → Redis → ES, API → RabbitMQ → Worker → ES → PG), we need end-to-end request tracing.

OpenTelemetry instrumentation:
- Auto-instrument FastAPI, SQLAlchemy, Redis, Elasticsearch, and aio-pika
- Propagate trace context via W3C `traceparent` header
- Custom spans for business logic (tenant resolution, cache lookup, queue publish)

A single search request trace would look like:

```
[API] search_documents (45ms)
  ├── [API] resolve_tenant (2ms)
  ├── [API] check_rate_limit (1ms)
  ├── [Redis] cache_get (1ms) → MISS
  ├── [ES] search (38ms)
  │     ├── query_build (1ms)
  │     └── es_request (37ms)
  └── [Redis] cache_set (1ms)
```

### Alerting

| Alert | Condition | Severity | Action |
|-------|-----------|----------|--------|
| High error rate | >5% 5xx in 5 min window | Critical | Page on-call, check dependency health |
| Search latency spike | p95 > 500ms for 5 min | Warning | Check ES cluster health, cache hit ratio |
| Queue backlog | Depth > 10K for 10 min | Warning | Scale up workers, check ES indexing throughput |
| Disk usage | ES or PG > 80% | Warning | Scale storage, archive old data |
| Tenant rate limit | Any tenant hitting limit repeatedly | Info | Review their usage, discuss plan upgrade |

---

## 5. Performance Optimization

### Elasticsearch Query Optimization

- **Avoid deep pagination.** `from + size` is O(n) in ES — page 1000 means ES scores and sorts 20,000 docs. Use `search_after` for deep pagination (cursor-based). Limit `from + size` to 10,000 max.
- **Index sorting.** Pre-sort indices by `created_at` descending. For "newest first" queries (common), ES can short-circuit evaluation and skip scoring documents it knows won't make the top-N.
- **Filter context vs. query context.** Tag filters and file_type filters go in `filter` (no scoring, cacheable by ES). Free-text goes in `must` (scored). Our current query already does this correctly.
- **Shard request cache.** ES caches the results of entire shard-level queries. Since most of our search traffic is read-heavy, this gives us a built-in cache layer inside ES itself.
- **Profile slow queries.** Use ES `_search` with `profile: true` to identify slow queries. Most performance issues come from wildcard queries on analyzed fields or excessive aggregation cardinality.

### Database Optimization

- **Connection pooling:** PgBouncer in transaction mode. Each app instance holds a pool of 10-20 connections instead of opening/closing per request. With 50 app instances, that's 500-1000 connections to PG — manageable.
- **Prepared statements:** SQLAlchemy supports server-side prepared statements. For our small set of queries (tenant lookup, document metadata by ID), this avoids re-parsing the query plan every time.
- **Read replicas for search metadata:** GET /documents/{id} hits a PG read replica. Only writes go to the primary.
- **Partial indexes:** `CREATE INDEX idx_active_tenants ON tenants(id) WHERE is_active = true`. We only ever query active tenants, so the index is smaller and faster.

### Indexing Throughput

- **Bulk API for ES:** Instead of single-document indexing, the worker batches documents (e.g., 100 at a time or every 500ms, whichever comes first) and uses ES `_bulk` API. This is 5-10x faster than individual requests.
- **Worker concurrency:** Each worker uses asyncio to process multiple messages concurrently (our current prefetch of 10 supports this). More workers = more throughput, linearly.
- **ES `refresh_interval`:** For heavy indexing bursts, temporarily set `refresh_interval` to 30s (from default 1s) on the target index. Documents become searchable slightly later, but indexing throughput jumps dramatically.

---

## 6. Operations

### Deployment Strategy

**Target: Kubernetes (EKS/GKE/AKS) with Helm charts.**

```
Production environment:
  ├── Namespace: docsearch-prod
  │   ├── Deployment: api (3+ replicas, HPA on CPU/latency)
  │   ├── Deployment: worker (2+ replicas, HPA on queue depth)
  │   ├── ConfigMap: app settings
  │   ├── Secret: DB passwords, API keys (from Vault)
  │   └── Service + Ingress: external traffic
  │
  ├── Managed Services:
  │   ├── Elasticsearch: Elastic Cloud or AWS OpenSearch (managed)
  │   ├── PostgreSQL: AWS RDS or Cloud SQL (managed, multi-AZ)
  │   ├── Redis: ElastiCache or Memorystore (managed, multi-AZ)
  │   └── RabbitMQ: CloudAMQP or Amazon MQ (managed)
```

Managed services for stateful components. We run only the stateless parts (API, worker) ourselves. Let the cloud provider handle replication, backups, and failover for databases.

### Zero-Downtime Deployments

- **Rolling updates:** Kubernetes default — new pods start, pass health checks, then old pods terminate. At no point are zero pods running.
- **Readiness probes:** Our `/v1/health` endpoint serves as the readiness probe. A pod isn't added to the load balancer until all dependencies are reachable.
- **Liveness probes:** Separate lightweight check (just returns 200). If the process is stuck, Kubernetes restarts it.
- **Blue-green for risky releases:** For major version bumps or database migrations, deploy the new version alongside the old one. Route a percentage of traffic to the new version, monitor error rates, then cut over fully.
- **Database migrations:** Run with tools like Alembic. Backward-compatible migrations only — add columns before the code that uses them, remove columns after the code that references them is fully deployed. Never rename a column in a single deployment.

### Backup & Recovery

| Component | Backup Strategy | RPO | RTO |
|-----------|----------------|-----|-----|
| **PostgreSQL** | Automated daily snapshots + continuous WAL archiving (Point-in-Time Recovery) | < 1 minute (PITR) | < 30 minutes |
| **Elasticsearch** | Snapshot to S3/GCS daily, plus before any major config change | < 24 hours | < 1 hour (restore from snapshot) |
| **Redis** | No backup (it's a cache, not a source of truth). Data reconstructs on restart. | N/A | N/A |
| **RabbitMQ** | Quorum queues replicate across nodes. Definitions exported daily. | ~0 (quorum replication) | < 10 minutes |

**Disaster recovery drill:** Run quarterly. Restore PG from PITR, ES from snapshot, verify data integrity, measure actual RTO against targets.

### Infrastructure as Code

- **Terraform** for cloud resources (VPC, RDS, ElastiCache, OpenSearch, ECS/EKS cluster)
- **Helm charts** for Kubernetes manifests
- **GitHub Actions** for CI/CD pipeline: lint → test → build image → push to ECR → deploy to staging → integration tests → deploy to prod

---

## 7. SLA — Achieving 99.95% Availability

99.95% uptime means at most **~22 minutes of downtime per month**. Here's how we get there.

### Error Budget Breakdown

| Category | Allocated Downtime | Mitigation |
|----------|-------------------|------------|
| **Planned maintenance** | 0 min | Zero-downtime deployments (rolling updates) |
| **Dependency failure** | ≤ 15 min/month | Multi-AZ deployments, automatic failover, graceful degradation |
| **Application bugs** | ≤ 5 min/month | Canary deployments, instant rollback, feature flags |
| **Infrastructure failure** | ≤ 2 min/month | Multi-AZ, auto-scaling, self-healing pods |

### What Makes 99.95% Achievable

1. **No single point of failure.** Every component runs in at least 2 availability zones. PG has a standby, ES has replica shards, Redis has a replica, RabbitMQ uses quorum queues.

2. **Fast detection.** Health checks every 10s, alerting within 1 minute of anomaly detection. The on-call engineer gets paged before most users notice.

3. **Fast recovery.** Kubernetes restarts crashed pods in seconds. Managed database failover completes in under 60 seconds. We don't need humans for most recovery — automation handles it.

4. **Graceful degradation.** When Redis goes down, we don't go down — we just get slower. When RabbitMQ goes down, we switch to synchronous indexing. The SLA is about the search being available and returning results, not about every subsystem being perfect.

5. **Deployment discipline.** No Friday deploys. Canary rollouts with automatic rollback if error rate spikes. Feature flags to decouple deployment from release.

### SLA Monitoring

- **External synthetic monitoring:** A probe service outside our infrastructure hits `/v1/health` and a sample search query every 30 seconds from multiple regions. This catches issues that internal monitoring might miss (DNS problems, load balancer misconfig, etc.).
- **SLA dashboard:** Real-time display of uptime percentage, error budget consumed, and burn rate. When we've consumed 50% of the monthly error budget, we freeze non-critical deployments.
- **Post-incident reviews:** Every incident that consumes error budget gets a blameless postmortem. Focus on what broke, what we can automate to prevent it, and what our monitoring missed.

---

## Summary

The gap between our prototype and a production system isn't a rewrite — it's a set of targeted additions:

| Area | Prototype Today | Production Target |
|------|----------------|-------------------|
| **Scale** | Single node, single worker | Multi-AZ, auto-scaled, managed services |
| **Resilience** | Fails if any dependency fails | Circuit breakers, fallbacks, graceful degradation |
| **Security** | Tenant ID header | JWT auth, mTLS, encryption at rest, audit logs |
| **Observability** | Console logging | Structured logs, metrics, distributed tracing, alerting |
| **Performance** | Untuned defaults | Bulk indexing, query optimization, connection pooling |
| **Operations** | Docker Compose | Kubernetes, IaC, CI/CD, zero-downtime deploys |
| **Availability** | Best-effort | 99.95% SLA with error budgets and synthetic monitoring |

The architecture was designed with these production concerns in mind from day one. The index-per-tenant model, the async write path, the two-tier caching, the stateless app servers — all of these exist because they make the production story straightforward. We're not retrofitting scalability; we're activating it.
