import json
import re

import httpx

from .config import validate_url
from .ha import MAX_RESPONSE, entity, time_range


def influx_ident(value):
    if not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid InfluxDB identifier")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def influx_string(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def flux_string(value):
    # Flux interpolates ${expressions} inside strings; JSON quoting alone is insufficient.
    if "${" in value or any(ord(c) < 32 for c in value) or len(value) > 256:
        raise ValueError("Unsupported Flux identifier")
    return json.dumps(value, ensure_ascii=False)


class Influx:
    def __init__(self, settings):
        self.s = settings

    async def query(self, query):
        if not self.s.influx_url:
            raise ValueError("InfluxDB is not configured")
        validate_url(self.s.influx_url)
        headers = {}
        if self.s.influx_token:
            headers["Authorization"] = "Token " + self.s.influx_token
        auth = (
            (self.s.influx_user, self.s.influx_password) if self.s.influx_user else None
        )
        async with httpx.AsyncClient(
            timeout=30, headers=headers, auth=auth, trust_env=False
        ) as client:
            if self.s.influx_version == "1":
                method, url = "GET", self.s.influx_url + "/query"
                kwargs = {"params": {"db": self.s.influx_database, "q": query}}
            elif self.s.influx_version == "2":
                method, url = "POST", self.s.influx_url + "/api/v2/query"
                kwargs = {
                    "params": {"org": self.s.influx_org},
                    "json": {"query": query, "type": "flux"},
                }
            else:
                raise ValueError("Supported InfluxDB versions: 1 and 2")
            try:
                async with client.stream(method, url, **kwargs) as response:
                    if response.status_code >= 300:
                        raise ValueError(
                            f"InfluxDB request failed ({response.status_code})"
                        )
                    result = bytearray()
                    async for chunk in response.aiter_bytes():
                        result.extend(chunk)
                        if len(result) > MAX_RESPONSE:
                            raise ValueError(
                                "InfluxDB response too large; narrow the interval"
                            )
            except httpx.HTTPError:
                raise ValueError("InfluxDB connection failed") from None
        if self.s.influx_version == "1":
            data = json.loads(result)
            if data.get("error") or any(
                "error" in row for row in data.get("results", [])
            ):
                raise ValueError(
                    "InfluxDB rejected the query; check database, measurement, and field"
                )
            return data
        # Return annotated CSV intact; do not misparse repeated table headers.
        return {"format": "annotated_csv", "data": result.decode()}

    async def measurements(self):
        if self.s.influx_version == "1":
            return await self.query("SHOW MEASUREMENTS LIMIT 500")
        return await self.query(
            'import "influxdata/influxdb/schema"\nschema.measurements(bucket: '
            + flux_string(self.s.influx_bucket)
            + ", start: -3650d) |> limit(n: 500)"
        )

    async def history(
        self, entity_id, measurement, field, start, end, aggregate, interval
    ):
        entity(entity_id)
        start, end = time_range(start, end, 366)
        if aggregate not in ("raw", "mean", "min", "max", "sum", "last"):
            raise ValueError("Unsupported aggregate")
        if not re.fullmatch(r"[1-9][0-9]{0,3}[smhd]", interval):
            raise ValueError("Interval must be a positive duration, such as 5m or 1h")
        domain, name = entity_id.split(".", 1)
        if self.s.influx_version == "1":
            value = influx_ident(field)
            expression = value if aggregate == "raw" else f"{aggregate}({value})"
            query = f'SELECT {expression} FROM {influx_ident(measurement)} WHERE time >= {influx_string(start)} AND time < {influx_string(end)} AND "domain" = {influx_string(domain)} AND "entity_id" = {influx_string(name)}'
            if aggregate != "raw":
                query += f" GROUP BY time({interval}) fill(none)"
            query += " LIMIT 5000"
        else:
            q = flux_string
            query = f"from(bucket: {q(self.s.influx_bucket)}) |> range(start: time(v: {q(start)}), stop: time(v: {q(end)})) |> filter(fn: (r) => r._measurement == {q(measurement)} and r._field == {q(field)} and r.domain == {q(domain)} and r.entity_id == {q(name)})"
            if aggregate != "raw":
                query += f" |> aggregateWindow(every: {interval}, fn: {aggregate}, createEmpty: false)"
            query += " |> limit(n: 5000)"
        return {
            "result": await self.query(query),
            "limit_per_series": 5000,
            "note": "At the limit, narrow the interval to retrieve remaining data. Uses Home Assistant's default domain/entity_id tag layout.",
        }
