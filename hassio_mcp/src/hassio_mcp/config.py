import json
import os
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Settings:
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: str = field(default="", repr=False)
    data_dir: Path = Path("data")
    config_dir: Path = Path("/homeassistant")
    esphome_config_dir: str = ""
    esphome_worker_url: str = ""
    esphome_worker_token: str = field(default="", repr=False)
    esphome_config_writes_enabled: bool = False
    esphome_install_enabled: bool = False
    yaml_files: tuple[str, ...] = ()
    config_files: tuple[str, ...] = ()
    writes_enabled: bool = False
    blueprint_writes_enabled: bool = False
    management_writes_enabled: bool = False
    ha_ca: str = ""
    ha_cert: str = ""
    ha_key: str = field(default="", repr=False)
    influx_url: str = ""
    influx_version: str = "1"
    influx_database: str = "homeassistant"
    influx_user: str = ""
    influx_password: str = field(default="", repr=False)
    influx_token: str = field(default="", repr=False)
    influx_org: str = ""
    influx_bucket: str = "homeassistant"

    @classmethod
    def from_env(cls):
        env = os.environ
        return cls(
            ha_url=env.get("HA_URL", "http://homeassistant.local:8123").rstrip("/"),
            ha_token=env.get("HA_TOKEN", ""),
            data_dir=Path(env.get("DATA_DIR", "data")),
            config_dir=Path(env.get("HA_CONFIG_DIR", "/homeassistant")),
            esphome_config_dir=env.get("ESPHOME_CONFIG_DIR", ""),
            esphome_worker_url=env.get("ESPHOME_WORKER_URL", ""),
            esphome_worker_token=env.get("ESPHOME_WORKER_TOKEN", ""),
            esphome_install_enabled=env.get("ALLOW_ESPHOME_INSTALL", "false").lower()
            == "true",
            esphome_config_writes_enabled=env.get(
                "ALLOW_ESPHOME_CONFIG_WRITES", "false"
            ).lower()
            == "true",
            yaml_files=tuple(json.loads(env.get("HA_YAML_FILES", "[]"))),
            config_files=tuple(json.loads(env.get("HA_CONFIG_FILES", "[]"))),
            writes_enabled=env.get("ALLOW_AUTOMATION_WRITES", "false").lower()
            == "true",
            blueprint_writes_enabled=env.get("ALLOW_BLUEPRINT_WRITES", "false").lower()
            == "true",
            management_writes_enabled=env.get(
                "ALLOW_MANAGEMENT_WRITES", "false"
            ).lower()
            == "true",
            ha_ca=env.get("HA_CA_FILE", ""),
            ha_cert=env.get("HA_CLIENT_CERT_FILE", ""),
            ha_key=env.get("HA_CLIENT_KEY_FILE", ""),
            influx_url=env.get("INFLUX_URL", "").rstrip("/"),
            influx_version=env.get("INFLUX_VERSION", "1"),
            influx_database=env.get("INFLUX_DATABASE", "homeassistant"),
            influx_user=env.get("INFLUX_USER", ""),
            influx_password=env.get("INFLUX_PASSWORD", ""),
            influx_token=env.get("INFLUX_TOKEN", ""),
            influx_org=env.get("INFLUX_ORG", ""),
            influx_bucket=env.get("INFLUX_BUCKET", "homeassistant"),
        )

    def tls(self):
        context = ssl.create_default_context(cafile=self.ha_ca or None)
        if bool(self.ha_cert) != bool(self.ha_key):
            raise ValueError("Both HA client certificate and key files are required")
        if self.ha_cert:
            context.load_cert_chain(self.ha_cert, self.ha_key)
        return context


def validate_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Configured URL must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "Configured URL must not contain credentials, query, or fragment"
        )
