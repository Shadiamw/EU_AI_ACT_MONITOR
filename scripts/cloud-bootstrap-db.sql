-- Idempotent roles + DB for cloud Postgres (Fly / Railway / etc.).
-- Run as a superuser (or role with CREATEROLE + CREATEDB).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monitor_owner') THEN
    CREATE ROLE monitor_owner LOGIN PASSWORD 'monitor_owner' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monitor_app') THEN
    CREATE ROLE monitor_app LOGIN PASSWORD 'monitor_app' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END
$$;

SELECT 'CREATE DATABASE monitor_test OWNER monitor_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'monitor_test')\gexec

\connect monitor_test
ALTER SCHEMA public OWNER TO monitor_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO monitor_app;
GRANT CONNECT ON DATABASE monitor_test TO monitor_app;
GRANT CONNECT ON DATABASE monitor_test TO monitor_owner;
