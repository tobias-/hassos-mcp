# PostgreSQL and TimescaleDB

Configure one trusted server using the PostgreSQL fields in the MCP app's Configuration tab. Credentials remain in HAOS options (or ignored `.env.local` for Compose); no tool accepts a connection URL or returns passwords. The SQL parser and driver are pinned. PostgreSQL is optional and no connections are made until requested.

## Roles and scope

Create a dedicated SELECT-only role. Grant CONNECT on the chosen database, USAGE on the required schemas and SELECT on explicit data tables and required metadata views. Do not use a superuser, database owner or a role with CREATEROLE, CREATEDB, replication or BYPASSRLS. Restrict PUBLIC/user-defined function privileges and only grant trusted views/types; database grants remain the security boundary for objects referenced indirectly by views and functions. Table allowlisting does not replace role permissions.

Set `postgres_host`, `postgres_port`, `postgres_database`, `postgres_user`, `postgres_password`, `postgres_sslmode` and `postgres_tables` (exact lowercase `schema.table` names). Use `require` for encrypted transport or `verify-full` with a trusted certificate and matching hostname. `disable` is for a trusted private server without TLS. Compose uses matching uppercase environment variables; `POSTGRES_TABLES` is a JSON list.

Use `postgres_inspect` for tables, extensions, hypertables, jobs, job status and compression settings. `postgres_query` accepts one SELECT or WITH SELECT with `$1`, `$2` parameters supplied in a separate JSON list. Data tables must be allowlisted; common PostgreSQL metadata and TimescaleDB informational views are built in. Only listed aggregate/date/size/JSON functions and built-in casts are supported. Custom functions, multi-statement SQL, data-modifying CTEs, SELECT INTO, locking reads and arbitrary commands are rejected. Transactions are READ ONLY and rolled back. No automatic retry.

Example: `postgres_query(query="SELECT * FROM timeseries.sensor_readings WHERE time >= $1::timestamptz ORDER BY time LIMIT 100", parameters=["2026-11-01T00:00:00Z"])`.

Limits: 32 KiB query/parameters, 100 parameters, 1–1000 rows, 64 KiB per row, 256 KiB response, 15-second statement timeout, 3-second lock timeout, 35-second overall deadline. Oversized rows/results stop with `truncated: true`; narrow the SELECT to fewer columns/time bounds. Audit records contain hashes/status, not raw query values or results. Common credential fields and known runtime credentials are withheld from returned rows; arbitrary unlabelled secrets cannot be recognized reliably.

## Human-approved columnstore maintenance

Configure a **separate** non-superuser maintenance role owning only the allowlisted hypertables, with its own `postgres_admin_user` and `postgres_admin_password`. Keep `allow_postgres_writes` off until ChatGPT asks every time for `postgres_apply_admin`.

For TimescaleDB 2.18+:

1. Inspect `extensions`, `hypertables`, compression settings and current jobs.
2. Call `postgres_propose_admin(operation="enable_columnstore", table="timeseries.sensor_readings", segmentby="sensor_id", orderby="time")`, choosing columns that actually exist and match the workload. Read the exact generated ALTER TABLE and effects. Human acceptance is required before `postgres_apply_admin` with its proposal ID/digest.
3. Separately propose `add_columnstore_policy` for the same table, `after_days=14`. Review and accept the generated `CALL public.add_columnstore_policy(...)` before applying. The Timescale extension must provide this procedure in `public`.
4. Inspect jobs/job_stats and compression settings afterwards. Conversion is lossless and does not configure data retention or delete raw rows. It can take locks and consume CPU/disk; it is not instantaneous.

Arbitrary administrative SQL, destructive statements, retention policies and shell access are intentionally unavailable. Each proposal is single-use, expires after 30 minutes, and binds the configured target, role, table identity/options and exact SQL. Errors never echo database exception text. If acknowledgement is lost during commit, inspect `management_change_status`, `postgres_audit_log` and database state: never repeat an uncertain write automatically. No automatic database rollback after a committed change.

References: [PostgreSQL role grants](https://www.postgresql.org/docs/current/sql-grant.html), [read-only transactions](https://www.postgresql.org/docs/current/sql-set-transaction.html), [columnstore settings](https://www.tigerdata.com/docs/reference/timescaledb/hypercore/alter_table), [columnstore policy](https://www.tigerdata.com/docs/reference/timescaledb/hypercore/add_columnstore_policy).
