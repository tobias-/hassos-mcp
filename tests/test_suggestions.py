from concurrent.futures import ThreadPoolExecutor

import pytest
from hassio_mcp.config import Settings
from hassio_mcp.suggestions import Suggestions


def test_persistent_concurrent_reports_and_pagination(tmp_path):
    settings = Settings(data_dir=tmp_path)
    box = Suggestions(settings)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: Suggestions(settings).submit(
                    "bug",
                    "History failed",
                    "Expected values; got an error",
                    "state_history",
                ),
                range(12),
            )
        )
    assert len({result["id"] for result in results}) == 1
    assert sum(result["status"] == "saved" for result in results) == 1
    feature = box.submit(
        "feature_request", "Compare series", "Align two series by timestamp"
    )
    bug = box.submit("bug", "Second bug", "Reproduction details")
    reopened = Suggestions(settings)
    first = reopened.list(limit=1)
    assert first["items"][0]["tool_name"] == "state_history"
    assert first["items"][0]["created_at"].endswith("+00:00")
    second = reopened.list(after_id=first["next_after_id"], limit=1)
    assert second["items"][0]["id"] == feature["id"]
    third = reopened.list(after_id=second["next_after_id"], limit=1)
    assert third["items"][0]["id"] == bug["id"]
    assert third["next_after_id"] is None
    assert len(reopened.list(kind="bug")["items"]) == 2
    assert reopened.list(after_id=bug["id"])["items"] == []
    assert box.path.stat().st_mode & 0o777 == 0o600


def test_known_credentials_redacted_before_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "private-runtime-key")
    box = Suggestions(Settings(data_dir=tmp_path, ha_token="private-ha-token"))
    box.submit("bug", "private-ha-token failed", "Authorization: private-runtime-key")
    row = box.list()["items"][0]
    assert row["title"] == "[REDACTED] failed"
    assert row["details"] == "Authorization: [REDACTED]"
    raw = box.path.read_bytes()
    assert b"private-runtime-key" not in raw
    assert b"private-ha-token" not in raw


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "other"},
        {"title": "  "},
        {"title": "x" * 201},
        {"details": ""},
        {"details": "x" * 8001},
        {"tool_name": "x" * 101},
    ],
)
def test_invalid_report_is_not_saved(tmp_path, change):
    box = Suggestions(Settings(data_dir=tmp_path))
    with pytest.raises(ValueError):
        box.submit(
            **({"kind": "bug", "title": "A bug", "details": "Evidence"} | change)
        )
    assert box.list()["items"] == []


@pytest.mark.parametrize(
    "options", [{"after_id": -1}, {"limit": 0}, {"limit": 101}, {"kind": "other"}]
)
def test_invalid_listing(tmp_path, options):
    with pytest.raises(ValueError):
        Suggestions(Settings(data_dir=tmp_path)).list(**options)
