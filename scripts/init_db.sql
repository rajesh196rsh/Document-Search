CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL UNIQUE,
    api_key     VARCHAR(512) NOT NULL,
    rate_limit  INTEGER DEFAULT 100,
    is_active   BOOLEAN DEFAULT true,
    config      JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE documents (
    id            UUID PRIMARY KEY,
    tenant_id     UUID NOT NULL REFERENCES tenants(id),
    title         VARCHAR(1024) NOT NULL,
    content_hash  VARCHAR(64),
    file_type     VARCHAR(50),
    status        VARCHAR(20) DEFAULT 'processing',
    metadata      JSONB DEFAULT '{}',
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_documents_tenant  ON documents(tenant_id);
CREATE INDEX idx_documents_status  ON documents(tenant_id, status);
CREATE INDEX idx_documents_created ON documents(tenant_id, created_at DESC);

-- Seed two demo tenants
INSERT INTO tenants (id, name, api_key, rate_limit) VALUES
    ('a1b2c3d4-e5f6-7890-abcd-ef1234567890', 'acme-corp', 'sk_acme_test_key_001', 100),
    ('b2c3d4e5-f6a7-8901-bcde-f12345678901', 'globex-inc', 'sk_globex_test_key_002', 50);
