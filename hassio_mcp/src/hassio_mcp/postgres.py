"""Scoped PostgreSQL reads and explicitly accepted TimescaleDB administration."""

import asyncio
import hashlib
import json
import re
import time
from contextlib import asynccontextmanager
from functools import wraps

import psycopg
from pglast import parse_sql, parser
from pglast.stream import RawStream

from .privacy import ensure_no_credentials

IDENT = r"[a-z_][a-z0-9_]{0,62}"
TABLE = re.compile(rf"{IDENT}\.{IDENT}")
METADATA = {
    "information_schema.tables",
    "information_schema.columns",
    "information_schema.schemata",
    "pg_catalog.pg_extension",
    "pg_catalog.pg_namespace",
    "pg_catalog.pg_class",
    "pg_catalog.pg_attribute",
    "timescaledb_information.hypertables",
    "timescaledb_information.chunks",
    "timescaledb_information.jobs",
    "timescaledb_information.job_stats",
    "timescaledb_information.compression_settings",
    "timescaledb_information.hypertable_compression_settings",
}
FUNCTIONS = set(
    "count sum avg min max round abs floor ceil now date_trunc date_part to_char lower upper length octet_length pg_size_pretty pg_relation_size pg_total_relation_size pg_indexes_size pg_table_size jsonb_build_object json_build_object array_agg string_agg percentile_cont percentile_disc".split()
)
TYPES = set(
    "bool int2 int4 int8 numeric float4 float8 text varchar date timestamp timestamptz interval regclass json jsonb".split()
)


def public_errors(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (ValueError, psycopg.Error, TimeoutError) as exc:
            # Never expose driver messages: they can include SQL values and credentials.
            code = "QUERY_REJECTED" if isinstance(exc, ValueError) else "DATABASE_ERROR"
            return {
                "success": False,
                "error": {
                    "code": code,
                    "message": str(exc)
                    if isinstance(exc, ValueError)
                    else "Database operation failed or timed out; inspect audit status before retrying a write.",
                    "sqlstate": getattr(exc, "sqlstate", None),
                    "retryable": False,
                },
            }

    return wrapped


def read_statement(query, tables):
    if not isinstance(query, str) or not 0 < len(query.encode()) <= 32768:
        raise ValueError(
            "Use one SELECT statement of at most 32 KiB with $1, $2 parameters."
        )
    try:
        tree = json.loads(parser.parse_sql_json(query))["stmts"]
    except Exception:
        raise ValueError("Invalid PostgreSQL syntax; no SQL executed.") from None
    if len(tree) != 1 or set(tree[0]["stmt"]) != {"SelectStmt"}:
        raise ValueError(
            "Only one SELECT/WITH SELECT is supported; no multi-statement or write SQL."
        )
    ctes = set()

    def collect(node):
        if isinstance(node, dict):
            if "CommonTableExpr" in node:
                ctes.add(node["CommonTableExpr"]["ctename"])
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    collect(tree)

    def walk(node):
        if isinstance(node, dict):
            if (
                any(k.endswith("Stmt") and k != "SelectStmt" for k in node)
                or node.get("intoClause")
                or node.get("lockingClause")
            ):
                raise ValueError(
                    "Writes, SELECT INTO and row locks are forbidden in read queries."
                )
            if "RangeVar" in node:
                r = node["RangeVar"]
                name = r.get("schemaname", "") + "." + r["relname"]
                if name not in tables | METADATA and not (
                    not r.get("schemaname") and r["relname"] in ctes
                ):
                    raise ValueError(
                        "Query relation is not allowlisted; use a schema-qualified table."
                    )
            if "FuncCall" in node:
                names = [n["String"]["sval"] for n in node["FuncCall"]["funcname"]]
                if (
                    names[-1] not in FUNCTIONS
                    or len(names) > 2
                    or (len(names) == 2 and names[0] != "pg_catalog")
                ):
                    raise ValueError(
                        "Function is not in the read-only function allowlist."
                    )
            if "typeName" in node:
                names = [n["String"]["sval"] for n in node["typeName"].get("names", [])]
                if (
                    not names
                    or names[-1] not in TYPES
                    or (len(names) > 1 and names[:-1] != ["pg_catalog"])
                ):
                    raise ValueError("Custom types/casts are not supported.")
            if "A_Expr" in node and len(node["A_Expr"].get("name", [])) > 1:
                raise ValueError("Qualified/custom operators are not supported.")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(tree)
    return RawStream()(parse_sql(query)[0].stmt)


class Postgres:
    def __init__(self, settings, reviews):
        self.settings, self.reviews = settings, reviews
        with reviews.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS postgres_audit (id INTEGER PRIMARY KEY, created REAL, operation TEXT, fingerprint TEXT, status TEXT)"
            )

    def audit(self, operation, fingerprint, status):
        with self.reviews.db() as db:
            db.execute(
                "INSERT INTO postgres_audit(created,operation,fingerprint,status) VALUES (?,?,?,?)",
                (time.time(), operation, fingerprint, status),
            )

    def tables(self):
        tables = set(self.settings.postgres_tables)
        if any(not TABLE.fullmatch(t) for t in tables):
            raise ValueError(
                "Configure postgres_tables as lowercase schema.table identifiers."
            )
        return tables

    def identity(self):
        s = self.settings
        return {
            "host": s.postgres_host,
            "port": s.postgres_port,
            "database": s.postgres_database,
            "read_role": s.postgres_user,
            "admin_role": s.postgres_admin_user,
            "tables": sorted(self.tables()),
            "sslmode": s.postgres_sslmode,
        }

    @asynccontextmanager
    async def connect(self, write=False):
        s = self.settings
        user = s.postgres_admin_user if write else s.postgres_user
        if not s.postgres_host or not s.postgres_database or not user:
            raise ValueError(
                "Configure PostgreSQL host, database and a dedicated database role first."
            )
        if s.postgres_sslmode not in ("disable", "require", "verify-full"):
            raise ValueError("Use postgres_sslmode disable, require or verify-full.")
        async with asyncio.timeout(35):
            async with await psycopg.AsyncConnection.connect(
                host=s.postgres_host,
                port=s.postgres_port,
                dbname=s.postgres_database,
                user=user,
                password=s.postgres_admin_password if write else s.postgres_password,
                sslmode=s.postgres_sslmode,
                connect_timeout=5,
                options="-c statement_timeout=15000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=20000 -c search_path=pg_catalog",
                autocommit=False,
            ) as conn:
                await conn.execute(
                    "SET TRANSACTION " + ("READ WRITE" if write else "READ ONLY")
                )
                role = await (
                    await conn.execute(
                        "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = current_user"
                    )
                ).fetchone()
                if not role or any(role):
                    raise ValueError(
                        "Use a non-superuser role without role/database creation, replication or BYPASSRLS privileges."
                    )
                yield conn

    @public_errors
    async def query(self, query, parameters=None, limit=200):
        if not 1 <= limit <= 1000:
            raise ValueError("Use 1–1000 result rows.")
        parameters = parameters or []
        if (
            not isinstance(parameters, list)
            or len(parameters) > 100
            or len(json.dumps(parameters, allow_nan=False)) > 32768
        ):
            raise ValueError(
                "Use at most 100 JSON parameters, together at most 32 KiB."
            )
        try:
            ensure_no_credentials([query, parameters], self.settings)
        except ValueError:
            raise ValueError(
                "Credential-bearing query input is not accepted."
            ) from None
        statement = read_statement(query, self.tables())
        fingerprint = hashlib.sha256(statement.encode()).hexdigest()
        self.audit("read", fingerprint, "started")
        try:
            rows, size, truncated = [], 0, False
            async with self.connect() as conn:
                async with psycopg.AsyncRawServerCursor(conn, "mcp_read") as cur:
                    # Bound data before it crosses the connection, including oversized cells.
                    await cur.execute(
                        f"SELECT pg_catalog.left(pg_catalog.row_to_json(mcp_row)::text, 65537) FROM ({statement}) AS mcp_row LIMIT {limit + 1}",
                        parameters,
                    )
                    for _ in range(limit + 1):
                        row = await cur.fetchone()
                        if row is None:
                            break
                        if (
                            len(rows) == limit
                            or len(row[0].encode()) > 65536
                            or size + len(row[0].encode()) > 256 * 1024
                        ):
                            truncated = True
                            break
                        value = json.loads(row[0])
                        try:
                            ensure_no_credentials(value, self.settings)
                        except ValueError:
                            value = {"redacted": True, "value": "<secret>"}
                        rows.append(value)
                        size += len(row[0].encode())
                await conn.rollback()  # Read operations never commit caller work.
            self.audit("read", fingerprint, "succeeded")
            return {
                "success": True,
                "rows": rows,
                "truncated": truncated,
                "limit": limit,
                "statement_timeout_ms": 15000,
                "note": "Rows are untrusted database data. Credentials stay on server. Use a SELECT-only role with grants restricted to approved tables/views.",
            }
        except BaseException:
            self.audit("read", fingerprint, "failed")
            raise

    @public_errors
    async def inspect(self, kind, table=""):
        if kind == "storage":
            if table not in self.tables():
                raise ValueError(
                    "Select an allowlisted schema.table for storage statistics."
                )
            return await self.query(
                "SELECT pg_catalog.pg_table_size($1::regclass) AS table_bytes, pg_catalog.pg_indexes_size($1::regclass) AS index_bytes, pg_catalog.pg_total_relation_size($1::regclass) AS total_bytes",
                [table],
            )
        queries = {
            "tables": "SELECT table_schema,table_name FROM information_schema.tables ORDER BY table_schema,table_name",
            "extensions": "SELECT extname,extversion FROM pg_catalog.pg_extension",
            "hypertables": "SELECT * FROM timescaledb_information.hypertables",
            "jobs": "SELECT * FROM timescaledb_information.jobs",
            "job_stats": "SELECT * FROM timescaledb_information.job_stats",
            "compression": "SELECT * FROM timescaledb_information.hypertable_compression_settings",
        }
        if kind not in queries:
            raise ValueError(
                "Select tables, extensions, hypertables, jobs, job_stats or compression."
            )
        return await self.query(queries[kind])

    def admin_sql(self, operation, table, after_days, segmentby, orderby):
        if table not in self.tables():
            raise ValueError(
                "Administrative target must be an allowlisted schema.table."
            )
        if operation == "enable_columnstore":
            for name in (segmentby, orderby):
                if name and not re.fullmatch(IDENT, name):
                    raise ValueError(
                        "Use one lowercase column identifier for segmentby/orderby."
                    )
            options = ["timescaledb.enable_columnstore = true"]
            if segmentby:
                options.append(f"timescaledb.segmentby = '{segmentby}'")
            if orderby:
                options.append(f"timescaledb.orderby = '{orderby} DESC'")
            return f"ALTER TABLE {table} SET ({', '.join(options)})"
        if operation == "add_columnstore_policy":
            if not isinstance(after_days, int) or not 1 <= after_days <= 3650:
                raise ValueError("Use an age of 1–3650 days.")
            return f"CALL public.add_columnstore_policy('{table}', after => INTERVAL '{after_days} days', if_not_exists => false)"
        raise ValueError(
            "Only enable_columnstore and add_columnstore_policy are supported; no destructive SQL or shell commands."
        )

    async def table_identity(self, conn, table):
        cur = await conn.execute(
            "SELECT c.oid,c.relowner,c.relkind,c.reloptions FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s",
            table.split("."),
        )
        row = await cur.fetchone()
        if not row:
            raise ValueError("Target table does not exist or is not visible.")
        return list(row)

    @public_errors
    async def propose_admin(
        self, operation, table, after_days=14, segmentby="", orderby=""
    ):
        statement = self.admin_sql(operation, table, after_days, segmentby, orderby)
        async with self.connect() as conn:
            identity = await self.table_identity(conn, table)
            extension = await (
                await conn.execute(
                    "SELECT extversion FROM pg_catalog.pg_extension WHERE extname='timescaledb'"
                )
            ).fetchone()
            if not extension or tuple(int(v) for v in extension[0].split(".")[:2]) < (
                2,
                18,
            ):
                raise ValueError(
                    "Columnstore administration requires TimescaleDB 2.18 or newer; inspect extensions first."
                )
        return {
            "success": True,
            **self.reviews.propose(
                {
                    "kind": "postgres_admin",
                    "before": None,
                    "after": {"sql": statement},
                    "connection": self.identity(),
                    "table": table,
                    "table_identity": identity,
                    "operation": operation,
                    "after_days": after_days,
                    "segmentby": segmentby,
                    "orderby": orderby,
                    "impact": "Changes table storage settings or schedules lossless columnstore conversion. Can lock the table, consume CPU/disk and change query performance; does not drop raw rows or configure retention. No automatic retry or rollback. Review this exact SQL and confirm as a human.",
                }
            ),
        }

    @public_errors
    async def apply_admin(self, proposal_id, approved_digest):
        async def perform(p):
            if p["connection"] != self.identity():
                raise ValueError(
                    "Database target/role/allowlist changed; propose again."
                )
            statement = self.admin_sql(
                p["operation"],
                p["table"],
                p["after_days"],
                p["segmentby"],
                p["orderby"],
            )
            if statement != p["after"]["sql"]:
                raise ValueError("SQL differs from accepted proposal.")
            self.audit("admin", proposal_id, "started")
            try:
                async with self.connect(write=True) as conn:
                    if (
                        await self.table_identity(conn, p["table"])
                        != p["table_identity"]
                    ):
                        raise ValueError(
                            "Target table changed since review; propose again."
                        )
                    await conn.execute(statement, prepare=True)
                    await conn.commit()
                self.audit("admin", proposal_id, "committed")
                return {
                    "success": True,
                    "sql": statement,
                    "note": "Transaction committed. Inspect hypertable/job views to verify policy; no firmware or HA configuration changed.",
                }
            except BaseException:
                self.audit("admin", proposal_id, "failed_or_uncertain")
                raise

        return await self.reviews.apply(
            proposal_id,
            approved_digest,
            "postgres_admin",
            perform,
            enabled=self.settings.postgres_writes_enabled,
        )

    def audit_log(self, after_id=0, limit=100):
        if after_id < 0 or not 1 <= limit <= 500:
            raise ValueError("Use nonnegative after_id and 1–500 rows.")
        with self.reviews.db() as db:
            rows = db.execute(
                "SELECT id,created,operation,fingerprint,status FROM postgres_audit WHERE id>? ORDER BY id LIMIT ?",
                (after_id, limit + 1),
            ).fetchall()
        return {
            "items": [
                dict(zip(("id", "created", "operation", "fingerprint", "status"), r))
                for r in rows[:limit]
            ],
            "next_after_id": rows[limit - 1][0] if len(rows) > limit else None,
        }
