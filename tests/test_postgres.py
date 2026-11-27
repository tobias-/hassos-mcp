import json
from contextlib import asynccontextmanager

import pytest
from hassio_mcp.config import Settings
from hassio_mcp.postgres import Postgres, read_statement
from hassio_mcp.review import ReviewStore

TABLES = {"timeseries.sensor_readings"}


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM timeseries.sensor_readings",
        "SELECT 1; SELECT 2",
        "WITH gone AS (DELETE FROM timeseries.sensor_readings RETURNING *) SELECT * FROM gone",
        "SELECT * INTO new_table FROM timeseries.sensor_readings",
        "SELECT * FROM timeseries.sensor_readings FOR UPDATE",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_terminate_backend(1)",
        "SELECT public.count(*) FROM timeseries.sensor_readings",
        "SELECT * FROM private.credentials",
        "SELECT * FROM sensor_readings",
        "SELECT 'x'::public.evil",
        "COPY timeseries.sensor_readings TO PROGRAM 'id'",
        "SELECT 1 OPERATOR(public.+) 2",
        "SELECT set_config('transaction_read_only','off',true)",
    ],
)
def test_read_ast_blocks_writes_commands_and_scope_escape(query):
    with pytest.raises(ValueError):
        read_statement(query, TABLES)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM timeseries.sensor_readings WHERE time > $1::timestamptz",
        "SELECT * FROM timescaledb_information.job_stats",
        "WITH readings AS (SELECT * FROM timeseries.sensor_readings) SELECT count(*) FROM readings",
        "SELECT ';DROP TABLE ignored' AS literal",
    ],
)
def test_parameterized_reads_and_metadata(query):
    assert read_statement(query, TABLES)


@pytest.fixture
def manager(tmp_path):
    settings = Settings(
        data_dir=tmp_path, postgres_tables=tuple(TABLES), postgres_writes_enabled=True
    )
    return Postgres(settings, ReviewStore(settings))


class Cursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class Connection:
    def __init__(self):
        self.calls = []
        self.commits = 0

    async def execute(self, query, *args, **kwargs):
        self.calls.append(query)
        return Cursor(("2.23.0",))

    async def commit(self):
        self.commits += 1


async def prepared(manager):
    conn = Connection()

    @asynccontextmanager
    async def connect(write=False):
        yield conn

    async def identity(*args):
        return [42, 5, "r", None]

    manager.connect = connect
    manager.table_identity = identity
    proposal = await manager.propose_admin(
        "add_columnstore_policy", "timeseries.sensor_readings", 14
    )
    assert proposal["success"] and proposal["approval_required"]
    assert "INTERVAL '14 days'" in proposal["after"]["sql"]
    assert not any("CALL " in x for x in conn.calls)
    return proposal, conn


async def test_admin_requires_exact_acceptance_and_rejects_replay(manager):
    p, conn = await prepared(manager)
    result = await manager.apply_admin(p["proposal_id"], "wrong")
    assert not result["success"] and not conn.commits
    result = await manager.apply_admin(p["proposal_id"], p["digest"])
    assert result["success"] and conn.commits == 1
    assert not (await manager.apply_admin(p["proposal_id"], p["digest"]))["success"]
    assert manager.audit_log()["items"][-1]["status"] == "committed"


@pytest.mark.parametrize("change", ["disabled", "target", "expired", "table"])
async def test_changed_or_disabled_admin_never_executes(manager, change):
    p, conn = await prepared(manager)
    if change == "disabled":
        manager.settings.postgres_writes_enabled = False
    if change == "target":
        manager.settings.postgres_host = "other-host"
    if change == "expired":
        with manager.reviews.db() as db:
            db.execute("UPDATE proposals SET created=0")
    if change == "table":

        async def identity(*args):
            return [43, 5, "r", None]

        manager.table_identity = identity
    assert not (await manager.apply_admin(p["proposal_id"], p["digest"]))["success"]
    assert not conn.commits


async def test_unconfigured_database_has_clear_sanitized_error(manager):
    result = await manager.query("SELECT 1")
    assert (
        not result["success"] and "Configure PostgreSQL" in result["error"]["message"]
    )
    manager.settings.postgres_password = "private-password"
    assert "private-password" not in repr(manager.settings)
    result = await manager.query("SELECT 'private-password'")
    assert "private-password" not in json.dumps(result)


@pytest.mark.parametrize(
    "operation,table,segment",
    [
        ("drop", "timeseries.sensor_readings", ""),
        ("enable_columnstore", "other.table", ""),
        ("enable_columnstore", "timeseries.sensor_readings", "a'; DROP TABLE x;"),
    ],
)
def test_admin_no_arbitrary_sql(manager, operation, table, segment):
    with pytest.raises(ValueError):
        manager.admin_sql(operation, table, 14, segment, "")
