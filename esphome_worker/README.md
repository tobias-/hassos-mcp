# Hassos MCP ESPHome worker

This companion app runs a pinned ESPHome 2026.10.2 toolchain, independently of ESPHome Builder. It has no Home Assistant configuration mount, Supervisor API permissions, USB devices or Docker socket. The MCP app sends bounded private configuration snapshots over an authenticated, private HTTP connection. Only referenced secrets are forwarded to the worker; they are not returned to ChatGPT.

## HAOS setup

1. Refresh this repository in the add-on store and install **Hassos MCP ESPHome worker**.
2. Generate a dedicated random token, for example `openssl rand -hex 32`. Set `worker_token` in the worker and the matching **ESPHome worker authentication token** in the MCP app. Do not reuse a HA/OpenAI key.
3. Start the worker. Copy its Hostname from Information. Set the MCP app's **ESPHome worker URL** to `http://HOSTNAME:6053`, save and restart the MCP app. No host port is published.
4. Confirm `esphome_validate` returns the installed toolchain version. The worker needs LAN access for device diagnostics and internet access for compiler packages. A `.local` name may not resolve across container networks; set the device's correct `wifi.use_address` in its source configuration through a reviewed change when needed.

For Compose, set `ESPHOME_WORKER_URL=http://esphome-worker:6053` and a dedicated `ESPHOME_WORKER_TOKEN` in `.env.local`, then run `docker compose --env-file .env.local --profile esphome up -d --build`. The worker receives only its own token, uses a private data volume, runs as UID 10001 with a read-only container filesystem, and has memory, CPU and process limits. Use HTTPS if connecting across an untrusted network; do not expose the worker to the internet.

## Supported runtime scope

Standalone ESP8266/ESP32 YAML is supported, including native components, substitutions, `!secret` and `!lambda`. Includes, packages, custom components, custom compiler hooks/options and local build inputs are deliberately rejected before toolchain execution. Flatten those configurations locally before using the managed runtime. Source reading remains available for broader configurations. The compiler never gets the live HA configuration directory or HA/tunnel credentials. Runtime operations do not rewrite the original file or restart HA/Builder.

`esphome_get_status` returns separate job `status` and device `availability`. Online requires a fresh native API handshake and device-info response. Failed authentication/name resolution yields unknown; timeout/refusal means the native API is unreachable, not necessarily that power is off. `esphome_get_logs` supports device subscriptions (up to eight seconds, 1–500 lines) and retained build/validation logs. Logs are bounded to 64 KiB and scrubbed against referenced secrets and credential patterns. Arbitrary unlabelled secrets in device messages cannot be recognized reliably; never intentionally log credentials.

`esphome_validate` uses isolated temporary snapshot files. Operations that take more than 20 seconds return `operation_id`; poll `esphome_operation_status` instead of repeating them. Persistent results include version, timestamps, bounded logs and explicit errors. One worker job runs at a time. Restarted jobs become unknown and are never restarted automatically. Worker data contains private snapshots and logs: include it in backups and protect those backups.
