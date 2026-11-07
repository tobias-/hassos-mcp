"""One bounded ESPHome invocation, isolated from MCP/HA credentials and config mounts."""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import resource
import shutil
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import yaml


def check_lambda(value):
    if not isinstance(value, str) or re.search(
        r"#|%:|\?\?=|\b(?:asm|__asm__?)\b", value
    ):
        raise ValueError(
            "Lambda preprocessing and inline assembly are not supported in the managed runtime"
        )


def guard_resolved(value, lambda_type):
    """Recheck after substitutions/secrets have produced the actual MCU source."""
    if isinstance(value, lambda_type):
        check_lambda(value.value)
    elif isinstance(value, dict):
        for child in value.values():
            guard_resolved(child, lambda_type)
    elif isinstance(value, list):
        for child in value:
            guard_resolved(child, lambda_type)


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
        "file",
        "files",
        "sdkconfig_options",
        "platform_version",
        "source",
        "path",
    }

    def walk(item):
        if getattr(item, "tag", None) == "!lambda":
            check_lambda(item.value)
        if isinstance(item, yaml.MappingNode):
            for key, value in item.value:
                if (
                    not isinstance(key, yaml.ScalarNode)
                    or key.value in blocked
                    or "$" in key.value
                ):
                    raise ValueError(
                        "Custom build hooks, dependencies and filesystem inputs are not supported"
                    )
                if key.value == "framework":
                    if not isinstance(value, yaml.MappingNode):
                        raise ValueError(
                            "Framework selection must be a literal mapping"
                        )
                    for option, setting in value.value:
                        if option.value == "version" and (
                            not isinstance(setting, yaml.ScalarNode)
                            or not re.fullmatch(
                                r"recommended|latest|dev|[0-9][a-zA-Z0-9_.-]*",
                                setting.value,
                            )
                        ):
                            raise ValueError(
                                "Custom framework sources are not supported"
                            )
                if key.value == "lambda":
                    check_lambda(value.value)
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


async def network(config, operation, lines, pinned_address=None, secret_values=()):
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
                pinned_address or CORE.address,
                int(api["port"]),
                type=socket.SOCK_STREAM,
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
            "success": operation == "status",
            "availability": "unknown",
            "method": "native_api",
            "address": None,
            "reason": "Configured device address could not be resolved; this does not establish offline status.",
            **(
                {
                    "error": {
                        "code": "DEVICE_OFFLINE",
                        "message": "Device address could not be resolved; no logs retrieved. Physical availability remains unknown.",
                        "retryable": False,
                    }
                }
                if operation == "logs"
                else {}
            ),
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
                    from worker import redact

                    text = redact(
                        message.message.decode("utf-8", "replace"), secret_values
                    )[:4096]
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
    from esphome.core import CORE, Lambda
    from esphome.log import setup_log

    setup_log(log_level="INFO")

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
    guard_resolved(config, Lambda)
    CORE.config = config
    if CORE.toolchain is None:
        from esphome.const import Toolchain

        CORE.toolchain = Toolchain.PLATFORMIO
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
            **asyncio.run(
                network(
                    config,
                    operation,
                    request.get("lines", 100),
                    secret_values=snapshot["secrets"].values(),
                )
            ),
            **metadata,
        }
    if operation == "compile":
        from esphome.__main__ import command_compile

        config["esphome"]["compile_process_limit"] = 1
        if (
            command_compile(
                SimpleNamespace(only_generate=False, skip_bootloader=False), config
            )
            != 0
        ):
            return {
                **failure(
                    "COMPILATION_FAILED",
                    "ESPHome compilation failed; inspect bounded logs.",
                ),
                **metadata,
            }
        binary = CORE.firmware_bin
        if (
            not binary.resolve().is_relative_to(directory.resolve())
            or not 0 < binary.stat().st_size <= 16 * 1024 * 1024
        ):
            return failure(
                "COMPILATION_FAILED",
                "Compiler artifact was missing, unsafe or too large.",
            )
        target = directory / "firmware.bin"
        shutil.copyfile(binary, target)
        target.chmod(0o440)
        return {
            "success": True,
            **metadata,
            "firmware_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "size_bytes": target.stat().st_size,
        }
    if operation == "install":
        binary = directory / "firmware.bin"
        if (
            hashlib.sha256(binary.read_bytes()).hexdigest()
            != request["firmware_sha256"]
        ):
            return failure(
                "CONFIG_CONFLICT", "Firmware hash changed before installation."
            )
        evidence = asyncio.run(
            network(config, "status", 1, pinned_address=request["address"])
        )
        if evidence.get("availability") != "online":
            return {
                **failure(
                    "DEVICE_OFFLINE",
                    "Native API preflight did not establish device identity; no OTA attempted.",
                ),
                "evidence": evidence,
                **metadata,
            }
        expected = request["expected_identity"]
        actual = evidence["identity"]
        if (
            not evidence.get("authenticated")
            or any(
                actual.get(key, "").lower() != expected.get(key, "").lower()
                for key in ("mac_address", "name", "model")
            )
            or actual["model"] != metadata["board"]
            or actual["name"] != metadata["name"]
            or evidence["address"] != request["address"]
        ):
            return failure(
                "ACCESS_DENIED",
                "Authenticated device identity, board, name or pinned address differs from approval; no OTA attempted.",
            )
        ota = [
            entry
            for entry in config.get("ota", [])
            if entry.get("platform") == "esphome"
        ]
        if len(ota) != 1:
            return failure(
                "CONFIG_INVALID",
                "Exactly one native ESPHome OTA configuration is required; no OTA attempted.",
            )
        from esphome import espota2

        ota = ota[0]
        noise_psk = ota.get("encryption", {}).get("key")
        address = ipaddress.ip_address(request["address"])
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        # Open exactly one socket and call the transport once. Do not use
        # run_ota/run_ota_impl_, which implement automatic retry/fallback loops.
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(20)
        try:
            connection.connect((str(address), int(ota["port"])))
        except OSError:
            connection.close()
            return failure(
                "INSTALLATION_FAILED",
                "OTA port connection failed before transfer; no firmware was sent.",
            )
        try:
            (directory / "transfer_started").write_text("single OTA attempt\n")
            print(
                "OTA transfer starting: exact approved artifact and identity verified",
                flush=True,
            )
            with connection, binary.open("rb") as firmware:
                espota2.perform_ota(
                    connection,
                    str(ota["password"]) if ota.get("password") else None,
                    firmware,
                    binary,
                    espota2.OTA_TYPE_UPDATE_APP,
                    str(noise_psk) if noise_psk else None,
                    False,
                    allow_plaintext_upload=False,
                )
        except Exception:
            return {
                **failure(
                    "OUTCOME_UNKNOWN",
                    "OTA transfer did not return a confirmed acknowledgement. Device may have changed; inspect it and never retry automatically.",
                ),
                **metadata,
            }
        return {
            "success": True,
            **metadata,
            "identity": actual,
            "firmware_sha256": request["firmware_sha256"],
            "address": str(address),
            "confirmation": "Device acknowledged the single OTA transfer. This does not verify post-reboot behavior or physical GPIO safety.",
        }
    return failure("ACCESS_DENIED", "Unsupported fixed operation.")


def main():
    # Private job directories inherit controller group 10001; compiler packages
    # inherit shared build group 2000. Other job UIDs cannot read private data.
    os.umask(0o007)
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (1800, 1800))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    directory = Path(sys.argv[2])
    try:
        result = run(sys.argv[1], directory)
    except Exception:
        # Exceptions from parsing/toolchains can contain expanded secrets.
        result = failure(
            "OUTCOME_UNKNOWN"
            if (directory / "transfer_started").exists()
            else "COMPILATION_FAILED"
            if sys.argv[1] == "compile"
            else "CONFIG_INVALID",
            "Operation failed; inspect the bounded logs and never blindly retry an uncertain installation.",
        )
    (directory / "result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
