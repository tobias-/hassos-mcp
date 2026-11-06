# Home Assistant Private MCP

A private MCP server for ChatGPT, running as a local HAOS app/add-on or Docker Compose service. OpenAI Secure MCP Tunnel initiates the connection outbound. The MCP process communicates with the tunnel over stdio; neither deployment publishes a host port.

## Capabilities

- Read all current entity values and attributes, with filtering and pagination.
- Read Recorder state history and long-term statistics, subject to retention and exclusions.
- Read InfluxDB 1.x or 2.x history through bounded queries, including aggregates.
- Read loaded UI/YAML/blueprint automation configuration, stored execution traces, and error logs.
- Propose and review exact changes, then create/edit UI automations or edit existing automations in explicitly allowlisted YAML files.

No generic service-call, delete, toggle, or manually-trigger-automation tool is exposed. Saving an automation can activate its triggers, so apply is marked destructive and non-read-only.

## Access and approval model

This deployment assumes a **single-user Platform organization and private ChatGPT workspace/app**. Access is enforced by OpenAI tunnel membership and permissions, not by an email address supplied to an MCP tool. Associate the tunnel only with your personal organization/workspace. Do not share the app or tunnel. If either context gains other users, revisit authentication before granting them access; this server does not independently authenticate ChatGPT user IDs.

Set ChatGPT to ask for confirmation **every time** `apply_automation_change` is used. Read the proposal's diff before approving. The tool requires the exact stored proposal ID and digest, rejects stale/expired proposals, and consumes each proposal once. These checks bind the operation to reviewed content; they do **not** cryptographically prove that a human clicked approval. ChatGPT's confirmation policy is the human approval boundary. Writes default to disabled until you set `allow_automation_writes` / `ALLOW_AUTOMATION_WRITES=true` after checking that policy.

Proposals expire after 30 minutes. Before/after configurations and status are persisted in a private SQLite database under `/data/state`. An interrupted write is not retried automatically. Use `automation_change_status`, read current HA state/configuration, and make a fresh proposal if needed. Treat the state volume as sensitive and include it in backups. Stop the old deployment before starting another with the same tunnel ID: run **one** tunnel instance.

## HAOS installation from GitHub (preferred)

[![Add this repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftobias-%2Fhassos-mcp)

Repository URL: `https://github.com/tobias-/hassos-mcp`

1. Open **Settings → Apps → Install app → ⋮ → Repositories** (called **Add-ons → Add-on Store** on older HAOS versions), add the repository URL, and install **Home Assistant Private MCP**. Use the app/add-on store; HACS does not install container apps.
2. HAOS builds the app from the self-contained `hassio_mcp/` directory. There is no container registry login or separate image publication step. Builds support `amd64` and `aarch64`; only the development host's architecture has been container-tested locally.
3. Set `tunnel_id` and `openai_api_key` in the app configuration. With `ha_token` empty, it uses HAOS's injected Supervisor token and the internal Core proxy. If your HA version denies an operation through that proxy, provide an HA long-lived access token for an administrator and `ha_url: http://homeassistant:8123`.
4. Configure InfluxDB using the **hostname shown on your InfluxDB add-on's information page**, port 8086, the installed major version, and credentials restricted to read access. For v1 use database/user/password; for v2 use org/bucket/token. Being an add-on alone does not identify its version. Custom Influx tag layouts and InfluxDB 3 are not supported yet.
5. For package-file editing, add relative filenames to `yaml_files`, for example `packages/lighting.yaml`. The config directory is mounted at `/homeassistant`. Start the app and connect ChatGPT as described below.

The GitHub repository must be published and reachable before the installation link works. Refresh the app store to discover new versions; installation of updates remains under your control. Maintainers must bump both `hassio_mcp/config.yaml` and `hassio_mcp/pyproject.toml` when releasing a change.

For manual installation instead, run `python3 scripts/package_addon.py`, then extract `build/hassio-mcp-haos.tar.gz` under HAOS `/addons/` and install it from Local apps. The source layout is the same for both routes.

HAOS stores app options under `/data/options.json`; the image contains no credentials. HAOS backups may contain these options. The local `.env.local` used during provisioning is ignored by Git and Docker and is not included in the app archive.

## Docker Compose fallback

The same image includes the pinned OpenAI tunnel client and Python MCP server. Put the tunnel key, tunnel ID, HA URL/token, and optional Influx settings in Git-ignored `.env.local`; `.env.example` lists settings. Keep the file mode `0600`.

```sh
docker compose --env-file .env.local up -d --build
docker compose logs --tail=80
```

Set `HA_URL` to your reachable Home Assistant address (default `http://homeassistant.local:8123`). For HTTPS with client certificates, mount the client certificate/key under `/run/secrets` and set `HA_CLIENT_CERT_FILE` / `HA_CLIENT_KEY_FILE`; set `HA_CA_FILE` only if a private CA is needed. Certificate verification stays enabled. The same TLS context is used for REST and WebSocket calls. A client certificate does not replace the HA access token.

To edit package YAML from Compose, mount the actual HA configuration directory at `/homeassistant` and set `HA_YAML_FILES` to a JSON array of relative filenames. UID 10001 needs write permission for those files and their containing directories. Without that mount, UI/`automations.yaml` API editing and loaded YAML inspection still work.

## Connect ChatGPT

Create a Secure MCP Tunnel in OpenAI Platform tunnel settings, associate it with your personal organization and ChatGPT workspace, and use its returned ID. The runtime key needs Tunnels Read + Use. A newly created project API key still needs suitable tunnel permissions; successful key creation alone does not establish them.

After the app is running, use ChatGPT **Plugins → Add custom MCP server → Connection: Tunnel** and select the tunnel. Keep write confirmation enabled. Test `connection_status`, `list_states`, `get_automation` and `automation_traces` before enabling automation writes. Verify a harmless proposed change is presented for approval before testing apply.

For the Compose deployment, local tunnel readiness can be inspected without publishing a port:

```sh
docker compose exec hassio-mcp python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/readyz").read().decode())'
```

## YAML editing and recovery limits

UI-managed edits go through Home Assistant's config API, which validates and saves `automations.yaml` and schedules a reload. Direct file editing accepts automation-list files or packages with a top-level `automation` list, requires an existing unique `id`, preserves tags/comments, and shows the complete file diff. It does not expand arbitrary includes or expose `secrets.yaml`. For blueprints, edit the automation's `use_blueprint` inputs, not the blueprint template.

For direct YAML files, the server first checks the existing configuration, atomically replaces the file, runs HA's full config check, and restores the original if validation fails and the file has not changed externally. It then reloads automations. A failed reload is reported as uncertain and retains the backup; it does not claim rollback succeeded.

The server detects changes since proposal creation and serializes its own writes. **Home Assistant's API has no conditional-write transaction shared with its UI, and external file editors do not share our lock.** Avoid simultaneous edits during apply; a narrow check/write race remains. API edits back up the target automation, while file edits back up the complete file. The API may reformat `automations.yaml`; its proposal diff describes the automation configuration, not that API-managed file formatting.

Backups are stored in `changes.sqlite3`, table `proposals`, JSON column `payload`, fields `before` and `after`. Restore only after inspecting the current configuration. A backup restore is itself a new automation change requiring approval; the server does not silently restore old configurations over later edits.

History queries enforce explicit timezone-aware ranges and response limits. Narrow a query if it exceeds the size limit. Recorder reads permit 31-day windows; statistics and Influx queries permit up to 366 days. Influx returns at most 5000 points per series; use smaller windows when that limit is reached. Measurements discovery is limited to 500 entries. Unrecorded or expired values cannot be recovered by this MCP.

## Development and validation

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -e ./hassio_mcp
.venv/bin/pytest -q
.venv/bin/ruff check hassio_mcp/src tests scripts
docker build --target compose -t hassio-mcp:0.1.0 hassio_mcp
docker build -t hassio-mcp-haos:0.1.0 hassio_mcp
```

Tests cover actual MCP stdio initialization/discovery, HTTP/WebSocket authentication, read failures, write annotations, proposal expiry/digest/replay/concurrency, stale edits, YAML rollback/path restrictions, and query input boundaries. These use simulated HA responses; a live HAOS/tunnel/ChatGPT acceptance check is still required before enabling writes.

Protocol references: [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), [tunnel client deployment](https://github.com/openai/tunnel-client/blob/main/docs/deployment/docker.md), [HA REST API](https://contributors.home-assistant.io/core/internals/rest-api/), [HA automation config API](https://github.com/home-assistant/core/blob/dev/homeassistant/components/config/automation.py), [HA trace API](https://github.com/home-assistant/core/blob/dev/homeassistant/components/trace/websocket_api.py), [InfluxQL](https://docs.influxdata.com/influxdb/v1/query_language/explore-data/), [InfluxDB v2 query API](https://docs.influxdata.com/influxdb/v2/query-data/execute-queries/influx-api/).
