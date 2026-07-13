-- ============================================================================
-- 001_extensions.sql
-- Runs automatically by the Postgres entrypoint the first time the data
-- directory is initialized (mounted at /docker-entrypoint-initdb.d).
--
-- Purpose: guarantee required extensions exist BEFORE the application issues
-- any DDL. The `vector` type (pgvector) and `gin_trgm_ops` operator class
-- (pg_trgm) must be present or the table/index creation in the app will fail.
--
-- gen_random_uuid() is core in PostgreSQL 13+, so no pgcrypto is needed.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector: embeddings + ANN indexes
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- trigram fuzzy matching for dedup
