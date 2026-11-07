"""One bounded ESPHome invocation, isolated from MCP/HA credentials and config mounts."""

import asyncio
import ipaddress
import json
import os
import resource
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml


def guard(source):
    # YAML must not grant shell execution, environment reads or filesystem inputs.
    depth = 0
    for count, event in enumerate(yaml.parse(source), 1):
        if isinstance(event, yaml.events.CollectionStartEvent):
            depth += 1
        if isinstance(event, yaml.events.CollectionEndEvent):
            depth -= 1
        if depth > 40 or count > 20000 or isinstance(event, yaml.events.AliasEvent):
            raise ValueError("Unsupported YAML complexity or aliases")
        tag = getattr(event, "tag", None)
        if (
            tag
            and not tag.startswith("tag:yaml.org,2002:")
            and tag not in ("!secret", "!lambda")
        ):
            raise ValueError(
                "Runtime accepts only !secret and !lambda tags; flatten includes locally"
            )
    node = yaml.compose(source)
    blocked = {
        "packages",
        "external_components",
        "platformio_options",
        "includes",
        "libraries",
        "build_path",
        "on_build",
        "on_compile",
        "extra_scripts",
        "sdkconfig_options",
        "platform_version",
        "source",
        "path",
    }

    def walk(item):
        if isinstance(item, yaml.MappingNode):
            for key, value in item.value:
                if not isinstance(key, yaml.ScalarNode) or key.value in blocked:
                    raise ValueError(
                        "Custom build hooks, dependencies and filesystem inputs are not supported"
                    )
                walk(value)
        elif isinstance(item, yaml.SequenceNode):
            for value in item.value:
                walk(value)

    walk(node)


def failure(code, message):
    return {
        "success": False,
        "error": {"code": code, "message": message, "retryable": False},
    }


async def network(config, operation, lines):
    from aioesphomeapi import APIClient
    from esphome.core import CORE

    if "api" not in config:
        return failure(
            "CONFIG_INVALID", "Native api is required for device identity and logs."
        )
    api = config["api"]
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                CORE.address, int(api["port"]), type=socket.SOCK_STREAM
            ),
            5,
        )
        address = addresses[0][4][0]
        ip = ipaddress.ip_address(address)
        if not ip.is_private or ip.is_loopback or ip.is_multicast or ip.is_unspecified:
            return failure(
                "ACCESS_DENIED", "Only private LAN device addresses are supported."
            )
    except (OSError, TimeoutError):
        return {
            "success": True,
            "availability": "unknown",
            "method": "native_api",
            "address": None,
            "reason": "Configured device address could not be resolved; this does not establish offline status.",
        }
    key = api.get("encryption", {}).get("key")
    client = APIClient(
        address,
        int(api["port"]),
        str(api.get("password", "")),
        noise_psk=str(key) if key else None,
        client_info="Hassos MCP",
        provide_time=False,
    )
    try:
        await asyncio.wait_for(client.connect(login=True), 8)
        info = await asyncio.wait_for(client.device_info(), 5)
        result = {
            "success": True,
            "availability": "online",
            "method": "native_api",
            "address": address,
            "authenticated": bool(key),
            "identity": {
                "name": info.name,
                "mac_address": info.mac_address,
                "model": info.model,
                "esphome_version": info.esphome_version,
            },
            "reason": "Live native API handshake and device_info response; no HA tracker was used.",
        }
        if operation == "logs":
            messages, complete = [], asyncio.Event()

            def receive(message):
                if len(messages) < lines:
                    text = message.message.decode("utf-8", "replace")[:4096]
                    messages.append(datetime.now(UTC).isoformat() + " " + text)
                if len(messages) >= lines:
                    complete.set()

            stop = client.subscribe_logs(receive, dump_config=False)
            try:
                await asyncio.wait_for(complete.wait(), 8)
            except TimeoutError:
                pass
            finally:
                stop()
            result.update(
                device_logs="\n".join(messages)[-65536:],
                source="device",
                truncated=len(messages) >= lines,
                window_seconds=8,
            )
        return result
    except Exception as exc:
        transport = type(exc).__name__ in (
            "SocketAPIError",
            "TimeoutAPIError",
            "TimeoutError",
        )
        return {
            "success": operation == "status",
            "availability": "offline" if transport else "unknown",
            "method": "native_api",
            "address": address,
            "reason": "Native API connection timed out or was refused; device power state is not known."
            if transport
            else "Native API authentication/protocol failed; availability and identity are unknown.",
            **(
                {
                    "error": {
                        "code": "DEVICE_OFFLINE" if transport else "ACCESS_DENIED",
                        "message": "Unable to retrieve authenticated device logs.",
                        "retryable": False,
                    }
                }
                if operation == "logs"
                else {}
            ),
        }
    finally:
        await client.disconnect()


def run(operation, directory):
    from esphome.config import read_config
    from esphome.const import __version__
    from esphome.core import CORE

    request = json.loads((directory / "request.json").read_text())
    snapshot = request["snapshot"]
    guard(snapshot["source"])
    (directory / "device.yaml").write_text(snapshot["source"])
    (directory / "secrets.yaml").write_text(yaml.safe_dump(snapshot["secrets"]))
    CORE.config_path = directory / "device.yaml"
    config = read_config({})
    if config is None:
        return {
            **failure(
                "CONFIG_INVALID",
                "ESPHome rejected the configuration; inspect bounded logs.",
            ),
            "version": __version__,
        }
    if CORE.target_platform not in ("esp8266", "esp32"):
        return failure(
            "CONFIG_INVALID",
            "Managed runtime currently supports ESP8266 and ESP32 only.",
        )
    metadata = {
        "version": __version__,
        "name": CORE.name,
        "platform": CORE.target_platform,
        "board": str(config[CORE.target_platform].get("board", "")),
    }
    if operation == "validate":
        return {"success": True, **metadata}
    if operation in ("status", "logs"):
        return {
            **asyncio.run(network(config, operation, request.get("lines", 100))),
            **metadata,
        }
    return failure("ACCESS_DENIED", "Unsupported fixed operation.")


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    directory = Path(sys.argv[2])
    try:
        result = run(sys.argv[1], directory)
    except Exception:
        # Exceptions from parsing/toolchains can contain expanded secrets.
        result = failure(
            "CONFIG_INVALID",
            "Unsupported or invalid ESPHome runtime input; inspect local configuration.",
        )
    (directory / "result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
