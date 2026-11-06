"""Reject credential-bearing management payloads rather than returning or backing them up."""

import os
import re

SENSITIVE = re.compile(
    r"(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|authorization|credentials|token|secret)$",
    re.I,
)


def ensure_no_credentials(value, settings):
    known = [
        item
        for item in (
            settings.ha_token,
            settings.influx_password,
            settings.influx_token,
            os.environ.get("OPENAI_API_KEY"),
            os.environ.get("CONTROL_PLANE_API_KEY"),
            os.environ.get("SUPERVISOR_TOKEN"),
        )
        if item and len(item) >= 8
    ]
    budget = [20000]

    def walk(item, depth=0):
        budget[0] -= 1
        if depth > 50 or budget[0] < 0:
            raise ValueError("Management content is too deeply nested or complex")
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    SENSITIVE.search(str(key))
                    and child not in (None, "")
                    and str(getattr(child, "tag", "")) != "!secret"
                ):
                    raise ValueError(
                        "Inline credential fields are excluded; move credentials to HA secrets and use !secret references"
                    )
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str) and (
            any(secret in item for secret in known)
            or re.search(
                r"-----BEGIN .*PRIVATE KEY-----|\bBearer\s+\S+|://[^/\s]+:[^/@\s]+@",
                item,
                re.I,
            )
        ):
            raise ValueError(
                "Credential-bearing content is excluded from management tools"
            )

    walk(value)
