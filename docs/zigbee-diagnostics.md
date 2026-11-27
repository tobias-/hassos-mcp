# Zigbee diagnostics

`zha_logs` reads a bounded ZHA/zigpy application log tail without changing logging. Use a full colon-separated 8-octet IEEE to select lines that mention one device. This is not an RF sniffer: absence of lines cannot prove a sleepy device is silent. `error_log` reads other Core logs through the same redaction and fallback path.

HAOS uses the Supervisor `/core/logs` endpoint with the app's Supervisor token and `homeassistant` API role. Compose uses the configured HA URL's `/api/hassio/core/logs` proxy and needs an administrator HA token. If unavailable, readers try the legacy `/api/error_log`, then `system_log/list` (warnings/errors only). Results name their source and coverage. Full log access is required before proposing a debug capture.

## Temporary debug capture

1. Set ChatGPT to **ask every time** for `zha_apply_debug` and `zha_apply_restore`. Keep other management write tools on ask every time too.
2. Enable `allow_zha_debug` in app configuration (`ALLOW_ZHA_DEBUG=true` in Compose) and restart. Reads need no write permission.
3. Call `zha_propose_debug` with 30–900 seconds and explicit `restore_levels`, for example `{"homeassistant.components.zha":"info","zigpy":"warning"}`. These are examples, not a discovered baseline. HA does not expose every module's previous level: a human must verify the restoration settings against their configuration and current logging session.
4. Show the exact debug scope, duration and restoration map to a human and obtain acceptance. Only then call `zha_apply_debug` with that proposal ID and digest. Acceptance includes the scheduled restoration.
5. Read `zha_capture_status` using the returned capture ID for retained lines and restoration outcome. To stop early, or recover after uncertain restoration, propose and explicitly accept `zha_propose_restore` / `zha_apply_restore`.

Only ZHA, zigpy and supported Zigbee radio namespaces are allowed. No global logger changes, Zigbee resets, device removal/re-pairing, log deletion or other destructive commands are added. All diagnostic mutations require human acceptance. Generic `logger.set_level` supports explicitly reviewed non-debug changes in these namespaces; use the timed workflow for debug.

The deadline, scope and restoration plan survive restart in the private management SQLite database. Cleanup runs while MCP is alive, on graceful shutdown, and after restart for overdue captures. It is **best effort**, not a hard timer when the app or HA is stopped. A failed or unacknowledged restoration is marked uncertain and never blindly repeated; inspect and obtain fresh human acceptance. Approved cleanup still runs if the write switch is later turned off. Avoid concurrent logging edits elsewhere, because restoration sets the accepted levels rather than detecting every intervening change.

At most one unresolved capture is allowed, with 100 retained capture records. Each retains at most 500 lines/64 KiB, polling a bounded Core log tail every five seconds; traffic bursts may be missed and duplicate lines are collapsed. Data remains in `/data/state` until locally archived/removed by an administrator. Credential-bearing lines and configured runtime secrets are withheld, including key/authorization/password lines; arbitrary unlabelled secret text cannot be reliably recognized. Treat logs and backups as private.

A proposal digest binds exact parameters, expiry and single use; it cannot prove a human clicked approval. ChatGPT's ask-every-time setting remains the human confirmation boundary.

[Home Assistant logger documentation](https://www.home-assistant.io/integrations/logger/)
