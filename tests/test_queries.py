import pytest

from hassio_mcp.config import Settings
from hassio_mcp.ha import entity, time_range
from hassio_mcp.influx import Influx, flux_string


def test_flux_interpolation_is_rejected():
    with pytest.raises(ValueError, match="Flux"):
        flux_string('${http.post(url: "https://example.com")}')


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-01-01", "2026-01-02"),
        ("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"),
        ("2026-01-01T00:00:00Z", "2026-05-01T00:00:00Z"),
    ],
)
def test_invalid_time_ranges(start, end):
    with pytest.raises(ValueError):
        time_range(start, end)


def test_entity_path_injection():
    with pytest.raises(ValueError):
        entity("sensor.test/../../services/light/turn_on")


@pytest.mark.parametrize("version", ["1", "2"])
async def test_influx_queries_are_bounded_and_no_raw_query_tool(version):
    influx = Influx(Settings(influx_version=version))
    queries = []

    async def capture(query):
        queries.append(query)
        return {}

    influx.query = capture
    await influx.history(
        "sensor.room",
        'x"; DROP DATABASE homeassistant; --',
        "value",
        "2026-01-01T00:00:00Z",
        "2026-01-02T00:00:00Z",
        "mean",
        "1h",
    )
    assert "5000" in queries[0]
    assert '\\"' in queries[0]
    with pytest.raises(ValueError):
        await influx.history(
            "sensor.room",
            "temperature",
            "value",
            "2026-01-01T00:00:00Z",
            "2026-01-02T00:00:00Z",
            "mean",
            "1h); delete()",
        )
