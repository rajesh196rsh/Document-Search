# Production Readiness Analysis

**Date:** April 2026

---

## 1. Scalability

Right now the prototype runs everything on one machine: single ES node, one app server, one worker. That obviously won't work at 100x scale (think 1B+ documents, 100K searches/sec). Here's what needs to change.

### Elasticsearch

The index-per-tenant model we already have is actually the right starting point. Each tenant gets its own index, so we can size them independently.

To get to 100x:

- **Shard sizing.** Rule of thumb is 10-50 GB per shard. A big tenant with 200M docs would need way more than the default 3 primary shards. We'd wire this into the tenant onboarding flow so it's automatic.
- **Separate node roles.** Right now everything runs on the same nodes. In production you'd split master-eligible, data, and coordinating nodes apart. Coordinating nodes handle the search fan-out without fighting for heap memory with indexing.
- **Hot-warm-cold tiers.** Most searches are against recent data. Put the last 30 days on fast SSDs, roll older stuff to cheaper warm/cold storage via ILM policies. Big cost savings.
- **More replicas for busy tenants.** Go from 1 replica to 2-3 for tenants with heavy read traffic. ES load-balances searches across replicas on its own.
- **Cross-cluster search** if we ever outgrow one cluster. Honestly, a well-tuned single cluster goes pretty far before you need this.

### App Servers and Workers

The app layer is already stateless. There's no session state on the server, tenant context comes from headers, cache lives in Redis. So scaling out is just "run more instances behind the load balancer."

For workers, same idea. Spin up more Celery/consumer instances on the same RabbitMQ queues. RabbitMQ round-robins messages across consumers, so throughput goes up linearly. In Kubernetes we'd set up HPA (horizontal pod autoscaler) keyed on p95 latency for the API and queue depth for workers.

### Database

PostgreSQL handles metadata, and at some point it'll need read replicas. Document detail lookups (GET by ID) can go to replicas, writes stay on primary. PgBouncer in front for connection pooling.

If the documents table gets past ~500M rows, we'd partition by `tenant_id`. Keeps scans fast and makes tenant offboarding trivial since you just drop the partition.

### Caching

Single-node Redis becomes Redis Cluster (6+ nodes, hash slot sharding) for both more memory and more throughput. The big thing to watch at scale is cache hit ratio. The difference between 80% and 95% hit rate is massive in terms of ES load, so we'd monitor that closely and tune TTLs per tenant based on their actual query patterns.

---

## 2. Resilience

Things will break. The question is what happens when they do.

### Circuit Breakers

If a dependency starts failing, we need to stop hammering it and give it time to recover. Each dependency gets its own circuit breaker:

| Dependency | When it trips | What we do instead |
|------------|---------------|-------------------|
| **Elasticsearch** | 5 failures in 30s | Serve from cache if we have it, otherwise 503 |
| **Redis** | 10 failures in 30s | Skip the cache layer, L1 in-memory still works. Let rate limiting slide. |
| **RabbitMQ** | 5 failures in 30s | Fall back to a local queue (file or SQLite), drain it later when RabbitMQ comes back |
| **PostgreSQL** | 3 failures in 30s | Nothing we can do. PG is the system of record. If it's down, we return 503. |

For implementation, `tenacity` handles retries (already in our deps). For the actual circuit breaker state, either `pybreaker` or a lightweight home-grown one backed by Redis so all instances share the breaker state.

### Retries

Different failures need different retry behavior. Transient stuff (timeouts, 502s) gets exponential backoff with jitter, starting at 100ms, capping at 5s, max 3 tries. 429s respect the `Retry-After` header. Client errors (400, 401, 404) don't get retried at all since they'll just fail again.

For the workers: 3 retries with backoff, then the message goes to the dead letter queue. We never drop messages silently.

### Failover

ES handles node failure on its own if you have replicas. A primary shard's node dies, the replica promotes. PostgreSQL failover via Patroni or RDS Multi-AZ, connection string follows the primary through a virtual IP. Redis uses Sentinel or ElastiCache Multi-AZ. RabbitMQ uses quorum queues for cross-node replication.

### Degradation Levels

We don't just go from "working" to "down." There's a spectrum:

```
Level 0: Everything up        → full functionality
Level 1: Redis gone           → no shared cache, no rate limiting, search is slower but works
Level 2: RabbitMQ gone        → indexing becomes synchronous, search unaffected
Level 3: ES partially down    → serve stale results from cache, better than nothing
Level 4: ES fully down        → search returns 503, but CRUD still works through PG
Level 5: PG down              → we're down, nothing safe to do
```

---

## 3. Security

### Auth

The prototype uses a simple `X-Tenant-ID` header with a database lookup. That's fine for now, but production needs proper auth.

For server-to-server integrations, we'd issue scoped API keys per tenant, hashed with bcrypt in the DB. Keys need to support rotation, so a tenant can have two active keys at the same time during a transition. For user-facing access, OAuth 2.0 with JWTs. The JWT carries tenant_id and permission claims, and we validate it locally using RS256 (no round-trip to the auth server on every request). Key rotation through a JWKS endpoint.

We'd also need RBAC within tenants. Not everyone should be able to delete documents. Basic roles: reader, editor, admin, enforced at the API middleware layer.

```
Request flow in production:
  Request → API Gateway (TLS termination)
          → Rate limit check
          → JWT validation (local)
          → Tenant resolution (from claims)
          → Permission check
          → Route handler
```

### Encryption

| Where | How |
|-------|-----|
| **In transit** | TLS 1.3 for external traffic, mTLS between internal services (via service mesh or cert-based auth) |
| **PG at rest** | Volume-level encryption (EBS encryption or GCP CMEK) |
| **ES at rest** | Volume encryption. Elastic's native encryption needs a Platinum license. |
| **Redis** | It's in-memory, but if persistence is on (RDB/AOF), encrypt the volume underneath |
| **Secrets** | Vault or AWS Secrets Manager. Not env vars. Not in the repo. |

### API Hardening

Pydantic already validates request bodies which is a good start. Beyond that:

- Cap content size at 10MB (configurable per tenant). Sanitize search queries so nobody can inject Elasticsearch DSL.
- Per-IP rate limiting at the gateway level, on top of the per-tenant rate limiting we already have.
- Strict CORS allow-list, no wildcards.
- Audit log for every write operation. Who did it, when, from where, what changed. Append-only table, separate from the rest of the data.

### Verifying Tenant Isolation

The architecture doc covers runtime isolation, but we'd also want automated tests that specifically try to access tenant A's data as tenant B. If it works, the build fails. We'd also log every DB and ES query with the tenant ID attached, and have a background job that flags any query missing a tenant scope. That's a bug, always.

---

## 4. Observability

### Metrics

Prometheus + Grafana. The metrics I'd prioritize, roughly in order of "what do I look at first when something's wrong":

- Request rate, error rate, latency (p50/p95/p99) per endpoint. The basics.
- Dependency health and latency for ES, PG, Redis, RabbitMQ. Is something slow or down?
- Cache hit ratio, broken out by L1 and L2. If this drops, ES load spikes.
- Search latency breakdown: how much time in cache check vs. ES query vs. serialization.
- Queue depth. If it's growing, either workers are too slow or ES is choking.
- ES disk usage per tenant index, PG table sizes, Redis memory, RabbitMQ message age.
- Per-tenant request rates. You want to catch noisy neighbors before they become a problem.

### Logging

Structured JSON, always. Every log line gets a `trace_id` and `tenant_id` so you can filter by tenant during incidents or follow a single request across services.

```json
{
  "timestamp": "2026-04-05T06:30:00.000Z",
  "level": "INFO",
  "service": "api",
  "trace_id": "abc123",
  "tenant_id": "acme-corp",
  "method": "GET",
  "path": "/v1/search",
  "status": 200,
  "latency_ms": 34,
  "cache_hit": true,
  "msg": "Search completed"
}
```

Important: we do NOT log document content (PII risk) or full request bodies (too noisy). INFO for request summaries and lifecycle events, WARN for retries and near-misses, ERROR for actual failures.

### Tracing

With requests bouncing between API, Redis, ES, RabbitMQ, and the worker, you need distributed tracing or you'll go insane debugging latency issues. OpenTelemetry with Jaeger or Tempo.

We'd auto-instrument FastAPI, SQLAlchemy, Redis, the ES client, and aio-pika. Trace context propagates via the W3C `traceparent` header. A typical search trace looks something like:

```
[API] search_documents (45ms)
  ├── resolve_tenant (2ms)
  ├── check_rate_limit (1ms)
  ├── [Redis] cache_get (1ms) → MISS
  ├── [ES] search (38ms)
  │     ├── query_build (1ms)
  │     └── es_request (37ms)
  └── [Redis] cache_set (1ms)
```

### Alerts

| What | When | Severity |
|------|------|----------|
| Error rate spike | >5% 5xx responses over 5 min | Critical, page on-call |
| Search getting slow | p95 > 500ms for 5 min | Warning, check ES and cache hit ratio |
| Indexing backlog | Queue depth > 10K for 10 min | Warning, scale workers |
| Running out of disk | ES or PG > 80% | Warning, scale or archive |
| Tenant hammering limits | Repeated 429s from one tenant | Info, review their plan |

---

## 5. Performance

### ES Query Tuning

The biggest gotcha is deep pagination. `from + size` in ES is O(n), so page 1000 means ES has to score and sort 20,000 documents just to give you 20. Use `search_after` (cursor-based) for anything past the first few pages, and hard-cap `from + size` at 10,000.

Other things that help: pre-sort indices by `created_at` descending so "newest first" queries can short-circuit early. Put tag/file_type filters in `filter` context (no scoring, cacheable by ES) and free-text in `must` context (scored). The prototype already does this correctly. ES also has its own shard request cache that we get for free on read-heavy workloads.

When something's slow, use `profile: true` on the `_search` API. Nine times out of ten it's a wildcard on an analyzed field or an aggregation with too much cardinality.

### Database

PgBouncer in transaction mode for connection pooling. Each app instance gets a pool of 10-20 connections. With 50 instances that's 500-1000 connections to PG, which is fine.

SQLAlchemy supports prepared statements on the server side. We only have a handful of query patterns (tenant lookup, doc metadata by ID), so the DB can cache the query plan and skip re-parsing every time.

For the tenants table: `CREATE INDEX idx_active_tenants ON tenants(id) WHERE is_active = true`. We only ever query active tenants, so a partial index keeps things tight.

### Indexing Speed

The single biggest improvement is switching from one-document-at-a-time indexing to ES bulk API. The worker batches up documents (say 100 at a time, or flushes every 500ms) and sends them in one `_bulk` request. That alone is 5-10x faster.

During heavy indexing bursts (like a big tenant uploading their entire doc library), we can temporarily bump ES `refresh_interval` from 1s to 30s on that tenant's index. Documents take a bit longer to become searchable, but indexing throughput goes way up.

---

## 6. Operations

### Deployment

We'd run this on Kubernetes (EKS, GKE, or AKS) with Helm charts.

```
docsearch-prod namespace:
  ├── api deployment (3+ replicas, HPA on CPU and latency)
  ├── worker deployment (2+ replicas, HPA on queue depth)
  ├── configmap for app settings
  ├── secrets from Vault
  └── service + ingress for external traffic

Managed services (we don't run these ourselves):
  ├── ES: Elastic Cloud or AWS OpenSearch
  ├── PG: RDS or Cloud SQL, multi-AZ
  ├── Redis: ElastiCache or Memorystore, multi-AZ
  └── RabbitMQ: CloudAMQP or Amazon MQ
```

The philosophy is: run the stateless stuff (API, workers) ourselves, let the cloud provider deal with the stateful stuff. They're better at replication, backups, and failover than we'll ever be for the cost.

### Zero-Downtime Deploys

Rolling updates are the default. New pods come up, pass health checks, old pods drain and terminate. Our `/v1/health` endpoint doubles as the readiness probe, so a pod doesn't get traffic until all its dependencies are reachable. Separate liveness probe that just returns 200; if the process hangs, Kubernetes kills and restarts it.

For risky releases (major versions, schema changes), we'd do blue-green: deploy the new version alongside the old, route a small percentage of traffic over, watch error rates, then cut over fully or roll back.

Database migrations go through Alembic. The rule is: always backward-compatible. Add a column before deploying the code that uses it. Remove a column after the code that referenced it is gone from all instances. Never rename a column in a single deploy.

### Backups

| What | How | RPO | RTO |
|------|-----|-----|-----|
| **PostgreSQL** | Daily snapshots + continuous WAL archiving for point-in-time recovery | < 1 min | < 30 min |
| **Elasticsearch** | Snapshot to S3 daily, plus before any big config change | < 24 hours | < 1 hour |
| **Redis** | Don't bother. It's a cache. It rebuilds itself. | N/A | N/A |
| **RabbitMQ** | Quorum queues handle replication. Export definitions daily as a safety net. | ~0 | < 10 min |

We'd run a disaster recovery drill quarterly. Actually restore PG from PITR, actually restore ES from a snapshot, verify the data, and measure whether our RTO targets are realistic or wishful thinking.

### IaC and CI/CD

Terraform for cloud infra (VPC, RDS, ElastiCache, OpenSearch, the K8s cluster itself). Helm charts for the application manifests. GitHub Actions for the pipeline: lint, test, build Docker image, push to ECR, deploy to staging, run integration tests, then deploy to prod.

---

## 7. SLA

99.95% availability means we can afford about 22 minutes of downtime per month. That's not a lot.

### Error Budget

| Category | Budget | How we stay under |
|----------|--------|-------------------|
| Planned maintenance | 0 min | Zero-downtime deploys, rolling updates |
| Dependency failures | ≤ 15 min/month | Multi-AZ everything, auto-failover, graceful degradation |
| Our bugs | ≤ 5 min/month | Canary deploys, instant rollback, feature flags |
| Infra failures | ≤ 2 min/month | Multi-AZ, auto-scaling, self-healing pods |

### Why 99.95% is Realistic

No single point of failure. Every component runs across at least 2 availability zones. PG has a standby, ES has replicas, Redis has a replica, RabbitMQ uses quorum queues.

Detection is fast. Health checks run every 10 seconds, alerts fire within a minute. Most of the time the on-call engineer gets paged before users start noticing.

Recovery is mostly automated. Kubernetes restarts crashed pods in seconds. Managed DB failover takes under 60 seconds. Humans aren't in the loop for most recovery scenarios.

The degradation model helps a lot here. When Redis dies, we don't go down, we just get a bit slower. When RabbitMQ dies, indexing switches to synchronous. The SLA is about search being available and returning results, not about every subsystem being perfect at all times.

And deployment discipline matters. No deploying on Fridays. Canary rollouts with auto-rollback if error rates spike. Feature flags so we can decouple "deployed" from "released."

### Monitoring the SLA

External synthetic checks from outside our infra, hitting `/v1/health` and a sample search query every 30 seconds from multiple regions. This catches stuff internal monitoring can miss (DNS issues, LB misconfiguration, etc.).

An SLA dashboard showing uptime %, error budget consumed, and burn rate. When we've used up 50% of the monthly budget, we freeze non-critical deploys.

Every incident that eats into the error budget gets a blameless postmortem. What broke, what we missed, what we can automate so it doesn't happen again.

---

## Summary

The prototype and a production system aren't that far apart. It's not a rewrite, it's a set of targeted additions:

| Area | Now | Production |
|------|-----|------------|
| **Scale** | Single node | Multi-AZ, auto-scaled, managed services |
| **Resilience** | Falls over if anything's down | Circuit breakers, fallbacks, degradation levels |
| **Security** | Tenant ID header | JWT, mTLS, encryption, audit logs |
| **Observability** | Console logs | Structured logs, Prometheus metrics, distributed tracing |
| **Performance** | Default settings | Bulk indexing, query tuning, connection pooling |
| **Ops** | Docker Compose | Kubernetes, Terraform, CI/CD, zero-downtime deploys |
| **Availability** | Best effort | 99.95% with error budgets and synthetic monitoring |

Most of the production story is already baked into the architecture. Index-per-tenant, async write path, two-tier cache, stateless app servers. These choices weren't accidental. They were made because they make scaling and operating this thing straightforward when the time comes.
