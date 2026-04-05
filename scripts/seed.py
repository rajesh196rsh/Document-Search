"""
Seed script — populates the system with sample documents via the API.
Run after docker-compose is up: python scripts/seed.py
"""

import httpx
import time

BASE_URL = "http://localhost:8000/v1"

ACME_TENANT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
GLOBEX_TENANT = "b2c3d4e5-f6a7-8901-bcde-f12345678901"

SAMPLE_DOCS = [
    {
        "tenant_id": ACME_TENANT,
        "doc": {
            "title": "Q4 2025 Financial Report",
            "content": "Revenue increased by 23% year-over-year driven by strong enterprise sales. Operating margins improved to 18.5%, up from 15.2% in Q3. Key growth areas include cloud services and professional consulting. The company exceeded its financial targets for the fiscal year.",
            "tags": ["finance", "quarterly-report"],
            "author": "jane.doe@acme.com",
            "file_type": "pdf",
            "metadata": {"department": "finance", "confidentiality": "internal"},
        },
    },
    {
        "tenant_id": ACME_TENANT,
        "doc": {
            "title": "Engineering Onboarding Guide",
            "content": "Welcome to the engineering team. This guide covers our development workflow, code review process, CI/CD pipeline setup, and deployment procedures. All new engineers should complete the security training module within the first two weeks. Our tech stack includes Python, PostgreSQL, Elasticsearch, and Redis.",
            "tags": ["engineering", "onboarding"],
            "author": "bob.smith@acme.com",
            "file_type": "docx",
            "metadata": {"department": "engineering", "confidentiality": "public"},
        },
    },
    {
        "tenant_id": ACME_TENANT,
        "doc": {
            "title": "Cloud Migration Strategy 2026",
            "content": "This document outlines our plan to migrate remaining on-premise workloads to AWS by end of 2026. Phase 1 covers database migration including PostgreSQL and Elasticsearch clusters. Phase 2 addresses application containerization using Docker and Kubernetes. Estimated cost savings of 35% over three years.",
            "tags": ["engineering", "cloud", "strategy"],
            "author": "alice.wong@acme.com",
            "file_type": "pdf",
            "metadata": {"department": "engineering", "confidentiality": "confidential"},
        },
    },
    {
        "tenant_id": ACME_TENANT,
        "doc": {
            "title": "Customer Support Playbook",
            "content": "Standard operating procedures for handling customer escalations. Tier 1 support handles password resets and basic troubleshooting. Tier 2 deals with integration issues and API errors. Tier 3 covers infrastructure problems and data recovery requests. SLA targets: Tier 1 within 4 hours, Tier 2 within 8 hours, Tier 3 within 24 hours.",
            "tags": ["support", "operations"],
            "author": "carol.jones@acme.com",
            "file_type": "docx",
            "metadata": {"department": "support"},
        },
    },
    {
        "tenant_id": ACME_TENANT,
        "doc": {
            "title": "Annual Security Audit Report 2025",
            "content": "The 2025 security audit identified 3 critical, 12 high, and 45 medium vulnerabilities. All critical items have been remediated. Key findings include outdated TLS configurations on two internal services and insufficient logging in the payment processing pipeline. Penetration testing confirmed no unauthorized data access was possible.",
            "tags": ["security", "audit", "compliance"],
            "author": "dave.chen@acme.com",
            "file_type": "pdf",
            "metadata": {"department": "security", "confidentiality": "restricted"},
        },
    },
    {
        "tenant_id": GLOBEX_TENANT,
        "doc": {
            "title": "Product Roadmap H1 2026",
            "content": "Globex product priorities for the first half of 2026. Key initiatives include the new search platform launch, mobile app redesign, and API v3 release. The search platform will support multi-language document indexing and real-time collaboration features. Target launch date is March 2026.",
            "tags": ["product", "roadmap"],
            "author": "emma.wilson@globex.com",
            "file_type": "pdf",
            "metadata": {"department": "product"},
        },
    },
    {
        "tenant_id": GLOBEX_TENANT,
        "doc": {
            "title": "Data Privacy Policy",
            "content": "Globex is committed to GDPR and CCPA compliance. All personal data is encrypted at rest using AES-256 and in transit using TLS 1.3. Data retention period is 3 years unless otherwise required by regulation. Users can request data export or deletion through the self-service portal. Annual privacy impact assessments are mandatory for all new features.",
            "tags": ["legal", "privacy", "compliance"],
            "author": "frank.garcia@globex.com",
            "file_type": "pdf",
            "metadata": {"department": "legal", "confidentiality": "public"},
        },
    },
    {
        "tenant_id": GLOBEX_TENANT,
        "doc": {
            "title": "Incident Postmortem: Search Outage Jan 2026",
            "content": "On January 15, 2026, the search service experienced a 47-minute outage due to an Elasticsearch cluster split-brain scenario. Root cause was a misconfigured minimum_master_nodes setting after a node replacement. Impact: approximately 12,000 failed search queries. Remediation: updated cluster configuration, added automated health monitoring, and implemented circuit breaker pattern in the API layer.",
            "tags": ["engineering", "incident", "postmortem"],
            "author": "emma.wilson@globex.com",
            "file_type": "docx",
            "metadata": {"department": "engineering", "confidentiality": "internal"},
        },
    },
]


def main():
    print("Seeding documents...\n")

    for item in SAMPLE_DOCS:
        tenant_id = item["tenant_id"]
        doc = item["doc"]

        try:
            resp = httpx.post(
                f"{BASE_URL}/documents",
                json=doc,
                headers={"X-Tenant-ID": tenant_id},
                timeout=10,
            )
            if resp.status_code == 202:
                data = resp.json()
                print(f"  ✓ [{tenant_id[:8]}...] {doc['title']} → id={data['id']}")
            else:
                print(f"  ✗ [{tenant_id[:8]}...] {doc['title']} → {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"  ✗ [{tenant_id[:8]}...] {doc['title']} → Error: {e}")

    print(f"\nDone. Seeded {len(SAMPLE_DOCS)} documents.")
    print("Wait a few seconds for the worker to index them, then try searching.")


if __name__ == "__main__":
    main()
