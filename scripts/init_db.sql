-- init_db.sql
-- Runs once when the PostgreSQL container is first created.
-- Creates the TimescaleDB extension and any initial DB-level configuration.
-- DO NOT use this for table creation — that is Alembic's job.

-- Enable TimescaleDB
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Enable uuid-ossp for gen_random_uuid() if needed
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Verify extensions
SELECT extname, extversion FROM pg_extension WHERE extname IN ('timescaledb', 'pgcrypto');
