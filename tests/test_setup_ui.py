import importlib.util
from html.parser import HTMLParser
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import httpx
from hassio_mcp.setup_content import GROUPS
from ruamel.yaml import YAML

from hassio_mcp import launcher, setup_ui

ROOT = Path(__file__).resolve().parents[1]


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs))


def test_native_configuration_and_linked_guide_cover_all_options():
    yaml = YAML(typ="safe")
    manifest = yaml.load((ROOT / "hassio_mcp/config.yaml").read_text())
    translations = yaml.load((ROOT / "hassio_mcp/translations/en.yaml").read_text())
    fields = [field for group in GROUPS for field in group["fields"]]
    assert {f["key"] for f in fields} == set(manifest["options"])
    assert set(translations["configuration"]) == set(manifest["options"])
    parser = PageParser()
    parser.feed(setup_ui.render_page().decode())
    urls = {link["href"] for link in parser.links}
    assert all(url in urls for field in fields for _, url in field["links"])
    assert not {"input", "form", "script"}.intersection(parser.tags)
    for link in parser.links:
        assert link["href"].startswith(("https://", "#"))
        if link["href"].startswith("https://"):
            assert link["rel"] == "noopener noreferrer"


def test_generated_docs_are_current():
    spec = importlib.util.spec_from_file_location(
        "setup_docs", ROOT / "scripts/generate_setup_docs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for path, content in module.generated_files().items():
        assert path.read_text() == content


def test_ingress_requires_trusted_peer_and_never_serves_files(monkeypatch):
    server = setup_ui.ThreadingHTTPServer(("127.0.0.1", 0), setup_ui.SetupHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("OPENAI_API_KEY", "secret-never-rendered")
    try:
        with httpx.Client(trust_env=False) as client:
            assert (
                client.get(
                    url, headers={"X-Forwarded-For": setup_ui.INGRESS_PROXY}
                ).status_code
                == 403
            )
            monkeypatch.setattr(setup_ui, "INGRESS_PROXY", "127.0.0.1")
            response = client.get(url)
            assert response.status_code == 200
            assert "secret-never-rendered" not in response.text
            assert "Open tunnel settings" in response.text
            assert response.headers["cache-control"] == "no-store"
            assert "default-src 'none'" in response.headers["content-security-policy"]
            assert client.get(url + "/data/options.json").status_code == 404
            assert client.post(url, json={"key": "secret"}).status_code == 501
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_missing_credentials_keep_guide_available_without_tunnel(monkeypatch):
    server = Mock()
    stop = Mock()
    stop.wait.side_effect = [False, True]
    monkeypatch.setattr(launcher, "start_setup_server", lambda: server)
    monkeypatch.setattr(launcher, "Event", lambda: stop)
    spawn = Mock(
        side_effect=AssertionError("must not start tunnel without credentials")
    )
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert launcher.run_haos(["CONTROL_PLANE_API_KEY"]) == 0
    spawn.assert_not_called()
    server.shutdown.assert_called_once()
    server.server_close.assert_called_once()


def test_tunnel_failure_stops_guide_and_reports_failure(monkeypatch):
    server = Mock()
    stop = Mock()
    stop.wait.return_value = False
    child = Mock()
    child.poll.return_value = 7
    child.returncode = 7
    spawn = Mock(return_value=child)
    monkeypatch.setattr(launcher, "start_setup_server", lambda: server)
    monkeypatch.setattr(launcher, "Event", lambda: stop)
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert launcher.run_haos([]) == 7
    spawn.assert_called_once_with(["tunnel-client", "run"], start_new_session=True)
    server.server_close.assert_called_once()


def test_shutdown_terminates_tunnel_process_group(monkeypatch):
    server = Mock()
    stop = Mock()
    stop.wait.return_value = True
    child = Mock()
    child.poll.return_value = None
    child.pid = 12345
    kill = Mock()
    monkeypatch.setattr(launcher, "start_setup_server", lambda: server)
    monkeypatch.setattr(launcher, "Event", lambda: stop)
    monkeypatch.setattr(launcher.subprocess, "Popen", Mock(return_value=child))
    monkeypatch.setattr(launcher.os, "killpg", kill)
    assert launcher.run_haos([]) == 0
    kill.assert_called_once_with(12345, launcher.signal.SIGTERM)
    child.wait.assert_called_once_with(timeout=10)
    server.server_close.assert_called_once()
