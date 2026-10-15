-- STUB for Part 4 (migration 0001 owns the real database roles).
-- Creates the two roles the ledger depends on and an empty test database.
-- Run once as a PostgreSQL superuser against the "postgres" database:
--   psql "postgresql://postgres:postgres@localhost:5432/postgres" -f tests/sql/bootstrap_roles.sql
-- The passwords are for the local test database only.

CREATE ROLE monitor_owner LOGIN PASSWORD 'monitor_owner' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE monitor_app LOGIN PASSWORD 'monitor_app' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE monitor_test OWNER monitor_owner;

\connect monitor_test
ALTER SCHEMA public OWNER TO monitor_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO monitor_app;
