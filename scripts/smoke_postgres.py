"""Exercise real PostgreSQL/TimescaleDB roles, bounds and approved maintenance."""

import asyncio
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
from hassio_mcp.config import Settings
from hassio_mcp.postgres import Postgres
from hassio_mcp.review import ReviewStore


async def run(port, directory):
    for _ in range(60):
        try:
            conn = await psycopg.AsyncConnection.connect(
                host="127.0.0.1",
                port=port,
                user="postgres",
                password="test-admin-fixture",
                autocommit=True,
            )
            break
        except psycopg.OperationalError:
            await asyncio.sleep(0.5)
    else:
        raise AssertionError("Test database did not become ready")
    async with conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        await conn.execute(
            "CREATE ROLE mcp_reader LOGIN PASSWORD 'test-reader-fixture'"
        )
        await conn.execute(
            "CREATE ROLE mcp_writer LOGIN PASSWORD 'test-writer-fixture'"
        )
        await conn.execute("CREATE SCHEMA timeseries AUTHORIZATION mcp_writer")
        await conn.execute("SET ROLE mcp_writer")
        await conn.execute(
            "CREATE TABLE timeseries.sensor_readings(time timestamptz NOT NULL, sensor_id text, value double precision)"
        )
        await conn.execute(
            "SELECT public.create_hypertable('timeseries.sensor_readings','time')"
        )
        await conn.execute(
            "INSERT INTO timeseries.sensor_readings SELECT now() - n * interval '1 day', 'fixture', n FROM generate_series(1,5) n"
        )
        await conn.execute("GRANT USAGE ON SCHEMA timeseries TO mcp_reader")
        await conn.execute("GRANT SELECT ON timeseries.sensor_readings TO mcp_reader")
    s = Settings(
        data_dir=Path(directory),
        postgres_host="127.0.0.1",
        postgres_port=port,
        postgres_database="postgres",
        postgres_user="mcp_reader",
        postgres_password="test-reader-fixture",
        postgres_admin_user="mcp_writer",
        postgres_admin_password="test-writer-fixture",
        postgres_sslmode="disable",
        postgres_tables=("timeseries.sensor_readings",),
        postgres_writes_enabled=True,
    )
    pg = Postgres(s, ReviewStore(s))
    r = await pg.query(
        "SELECT sensor_id,value FROM timeseries.sensor_readings WHERE value > $1 ORDER BY value",
        [1],
        limit=2,
    )
    assert r.get("success") and r["truncated"] and len(r["rows"]) == 2, r
    assert r["rows"][0]["value"] == 2, r
    assert (await pg.inspect("extensions"))["success"]
    for operation in ("enable_columnstore", "add_columnstore_policy"):
        p = await pg.propose_admin(
            operation, "timeseries.sensor_readings", 14, "sensor_id", "time"
        )
        assert p.get("success"), p
        assert not (await pg.apply_admin(p["proposal_id"], "unapproved"))["success"]
        r = await pg.apply_admin(p["proposal_id"], p["digest"])
        assert r.get("success"), r
        assert not (await pg.apply_admin(p["proposal_id"], p["digest"]))["success"]
    jobs = await pg.inspect("jobs")
    assert jobs.get("success") and any(
        r.get("proc_name") == "policy_compression" for r in jobs["rows"]
    ), jobs
    r = await pg.query("SELECT count(*) AS n FROM timeseries.sensor_readings")
    assert r["rows"][0]["n"] == 5, r
    assert not (await pg.query("SELECT pg_read_file('/etc/passwd')"))["success"]
    s.postgres_user = "postgres"
    s.postgres_password = "test-admin-fixture"
    assert not (await pg.query("SELECT 1"))["success"]
    print(
        "Real TimescaleDB: read-only role, native parameters, result cap, SQL rejection, exact approvals, columnstore settings/policy, replay rejection and retained rows passed"
    )


def main():
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "-e",
            "POSTGRES_PASSWORD=test-admin-fixture",
            "-p",
            "127.0.0.1::5432",
            "timescale/timescaledb:2.23.0-pg17",
        ],
        text=True,
    ).strip()
    try:
        info = json.loads(
            subprocess.check_output(["docker", "inspect", container], text=True)
        )[0]
        port = int(info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"])
        with TemporaryDirectory(prefix="hassos-postgres-") as directory:
            asyncio.run(run(port, directory))
    finally:
        subprocess.run(
            ["docker", "stop", "-t", "2", container],
            check=True,
            stdout=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main()
