# Home Assistant Private MCP

A private MCP server for ChatGPT, running as a local HAOS app/add-on or Docker Compose service. OpenAI Secure MCP Tunnel initiates the connection outbound. The MCP process communicates with the tunnel over stdio; neither deployment publishes a host port.

## Capabilities

- Read all current entity values and attributes, with filtering and pagination.
- Read Recorder state history and long-term statistics, subject to retention and exclusions.
- Read InfluxDB 1.x or 2.x history through bounded queries, including aggregates.
- Read loaded UI/YAML/blueprint automation configuration, stored execution traces, and error logs.
- List/inspect/enable/disable/trigger/reload automations directly, without a proposal or additional agreement.
- Propose and review exact changes, then create/edit/delete UI automations or entries in existing explicitly allowlisted YAML files.
- Save and review persistent suggestions for missing capabilities and observed bugs.
- List/read automation, script, and template blueprints; propose create/update/delete and apply only after explicit acceptance.

Automation controls have dedicated tools with specific entity targets. Configuration changes use reviewed proposals. Mutating controls and apply tools are truthfully marked non-read-only; tool annotations do not bypass client permission settings.

## Access and approval model

Automation enable, disable, trigger, inspect, and reload are preauthorized. The server and its LLM instructions require no proposal or additional agreement for those operations. Configure your client's permissions to allow those tools directly while keeping both `apply_automation_change` and `apply_blueprint_change` on **ask every time**. The server cannot override ChatGPT's own confirmation policy. The write switches below govern configuration changes only, not these direct controls.

This deployment assumes a **single-user Platform organization and private ChatGPT workspace/app**. Access is enforced by OpenAI tunnel membership and permissions, not by an email address supplied to an MCP tool. Associate the tunnel only with your personal organization/workspace. Do not share the app or tunnel. If either context gains other users, revisit authentication before granting them access; this server does not independently authenticate ChatGPT user IDs.

Set ChatGPT to ask for confirmation **every time** `apply_automation_change` is used. Read the proposal's diff before approving. The tool requires the exact stored proposal ID and digest, rejects stale/expired proposals, and consumes each proposal once. These checks bind the operation to reviewed content; they do **not** cryptographically prove that a human clicked approval. ChatGPT's confirmation policy is the human approval boundary. Writes default to disabled until you set `allow_automation_writes` / `ALLOW_AUTOMATION_WRITES=true` after checking that policy.

Blueprint modifications have the same confirmation boundary: configure **ask every time** for `apply_blueprint_change`, including creation and deletion. They have a separate `allow_blueprint_writes` / `ALLOW_BLUEPRINT_WRITES` switch, defaulting to false. General permission to manage blueprints is not acceptance of a particular change.

Proposals expire after 30 minutes. Before/after configurations and status are persisted in a private SQLite database under `/data/state`. An interrupted write is not retried automatically. Use `automation_change_status`, read current HA state/configuration, and make a fresh proposal if needed. Treat the state volume as sensitive and include it in backups. Stop the old deployment before starting another with the same tunnel ID: run **one** tunnel instance.

## HAOS installation from GitHub (preferred)

[![Add this repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftobias-%2Fhassos-mcp)

Repository URL: `https://github.com/tobias-/hassos-mcp`

1. Open **Settings → Apps → Install app → ⋮ → Repositories** (called **Add-ons → Add-on Store** on older HAOS versions), add the repository URL, and install **Home Assistant Private MCP**. Use the app/add-on store; HACS does not install container apps.
2. HAOS builds the app from the self-contained `hassio_mcp/` directory. There is no container registry login or separate image publication step. Builds support `amd64` and `aarch64`; only the development host's architecture has been container-tested locally.
3. Start the add-on and select **Open Web UI** for the linked setup guide. It stays available even before credentials are configured. Follow its links to obtain `tunnel_id` and `openai_api_key`, enter them in the Configuration tab, then save and restart. With `ha_token` empty, HAOS supplies automatic authentication. If a required operation is denied, the guide explains how to obtain an administrator token for a manual connection.
4. Configure InfluxDB using the **hostname shown on your InfluxDB add-on's information page**, port 8086, the installed major version, and credentials restricted to read access. For v1 use database/user/password; for v2 use org/bucket/token. Being an add-on alone does not identify its version. Custom Influx tag layouts and InfluxDB 3 are not supported yet.
5. For package-file editing, add relative filenames to `yaml_files`, for example `packages/lighting.yaml`. The config directory is mounted at `/homeassistant`. Start the app and connect ChatGPT as described below.

The GitHub repository must be published and reachable before the installation link works. Refresh the app store to discover new versions; installation of updates remains under your control. Maintainers must bump both `hassio_mcp/config.yaml` and `hassio_mcp/pyproject.toml` when releasing a change.

For manual installation instead, run `python3 scripts/package_addon.py`, then extract `build/hassio-mcp-haos.tar.gz` under HAOS `/addons/` and install it from Local apps. The source layout is the same for both routes.

HAOS stores app options under `/data/options.json`; the image contains no credentials. HAOS backups may contain these options. The local `.env.local` used during provisioning is ignored by Git and Docker and is not included in the app archive.

The setup guide is available through authenticated Home Assistant ingress only; port 8099 is not published to the host. It shows public instructions and links, never your saved values. The native Configuration tab has readable field labels and help text. Its descriptions are plain text, so use Web UI or Documentation for clickable links. Existing configuration keys and values are preserved when upgrading from 0.1.0.

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

## Persistent suggestion box

The LLM can call `submit_suggestion` when a capability is missing or a tool appears broken. Reports have a kind (`feature_request` or `bug`), title, details, optional affected tool, server-assigned ID, and UTC timestamp. Server instructions encourage actionable reports with a use case or expected/actual behavior and reproduction steps. They also tell the LLM to check existing tools before reporting a missing capability. Reporting is advisory; it does not fix anything or publish GitHub issues.

Ask **“Show me the saved suggestions and bug reports”** to use `list_suggestions`. It returns oldest first, optionally filtered by kind, with `next_after_id` for further pages. Exact repeat submissions return the original ID. Reports are retained without automatic expiry or deletion, and are unverified caller observations.

The log is a separate SQLite file at `/data/state/suggestions.sqlite3` in both HAOS and Compose (or `$DATA_DIR/suggestions.sqlite3` with a custom data directory). Committed reports survive MCP restarts, container replacement, and add-on upgrades as long as the persistent data is retained. Include the add-on data or Compose `mcp-data` volume in backups; uninstalling the add-on or deleting its data volume can remove reports. This is independent of the rotating container log. The file can also be inspected offline with SQLite: `SELECT id, created_at, kind, title, details, tool_name FROM suggestions ORDER BY id;`.

Both tools work with automation writes disabled and HA unavailable. Submitting is correctly marked as a local write, so client permission settings still apply. Reports should omit credentials and unnecessary household data; the server additionally redacts known configured tokens/passwords, but cannot recognize every possible secret in free text. Nothing automatically copies raw HA logs or conversations into the suggestion box.

After upgrading to 0.3.0, refresh the plugin's tools in ChatGPT to discover the two new tools.

## YAML editing and recovery limits

Automation configuration supports create, update, and delete after acceptance. `propose_automation_delete` prepares a deletion; `apply_automation_change` applies its reviewed digest. YAML operations modify only the selected entry in an existing allowlisted automation list or package, leaving other entries and package sections intact. New YAML entries require a unique ID and an existing allowlisted destination; arbitrary new configuration files are not created by these tools.

## Direct automation controls

| Tool | Behavior |
| --- | --- |
| `list_automations` | Discover automation entity IDs, enabled state, attributes and timestamps. |
| `inspect_automation` | Read state, loaded configuration, and optional trace summaries. |
| `enable_automation` | Enable one automation's triggers. |
| `disable_automation` | Disable one automation; `stop_actions=true` stops running actions, or use false to let them finish. |
| `trigger_automation` | Run one automation, even when disabled; conditions are checked unless `skip_condition=true`. |
| `reload_automations` | Reload existing automation configuration globally, without writing new configuration. |

These controls work with `allow_automation_writes=false`. They never approve a pending configuration proposal. Trigger/reload outcomes may be uncertain on network failures; inspect state/traces before retrying. A completed service request is not proof that every action succeeded. Reloading can stop runs and reset waits for changed automations. See [trigger semantics](https://www.home-assistant.io/actions/automation.trigger/), [disable semantics](https://www.home-assistant.io/actions/automation.turn_off/), and [reload behavior](https://www.home-assistant.io/actions/automation.reload/).

Update to **0.5.0** and refresh ChatGPT's tool list to discover these controls.

## Configuration validation and recovery

UI-managed edits go through Home Assistant's config API, which validates and saves `automations.yaml` and schedules a reload. Direct file editing accepts automation-list files or packages with a top-level `automation` list, requires an existing unique `id`, preserves tags/comments, and shows the complete file diff. It does not expand arbitrary includes or expose `secrets.yaml`. Automation tools edit a blueprint consumer's `use_blueprint` inputs; use the separate blueprint tools below to modify the shared template.

For direct YAML files, the server first checks the existing configuration, atomically replaces the file, runs HA's full config check, and restores the original if validation fails and the file has not changed externally. It then reloads automations. A failed reload is reported as uncertain and retains the backup; it does not claim rollback succeeded.

The server detects changes since proposal creation and serializes its own writes. **Home Assistant's API has no conditional-write transaction shared with its UI, and external file editors do not share our lock.** Avoid simultaneous edits during apply; a narrow check/write race remains. API edits back up the target automation, while file edits back up the complete file. The API may reformat `automations.yaml`; its proposal diff describes the automation configuration, not that API-managed file formatting.

Backups are stored in `changes.sqlite3`, table `proposals`, JSON column `payload`, fields `before` and `after`. Restore only after inspecting the current configuration. A backup restore is itself a new automation change requiring approval; the server does not silently restore old configurations over later edits.

History queries enforce explicit timezone-aware ranges and response limits. Narrow a query if it exceeds the size limit. Recorder reads permit 31-day windows; statistics and Influx queries permit up to 366 days. Influx returns at most 5000 points per series; use smaller windows when that limit is reached. Measurements discovery is limited to 500 entries. Unrecorded or expired values cannot be recovered by this MCP.

## Blueprint CRUD and acceptance

1. Use `list_blueprints` for installed metadata/inputs and load errors in the `automation`, `script`, or `template` domain. Use `get_blueprint` for the exact YAML at a relative path such as `author/name.yaml`.
2. Call `propose_blueprint_change` with `operation: create`, `update`, or `delete`. Create/update require complete `config_yaml`; delete must omit it. The proposal stores the before/after content and returns the exact diff without modifying HA.
3. Show the user the operation, path, diff, and effects on consumers. **Wait for explicit acceptance of that proposal.** Only then call `apply_blueprint_change` with its `proposal_id` and `approved_digest`. Keep the client's confirmation enabled on every apply call.
4. Inspect `blueprint_change_status` and the current blueprint after applying. A proposal expires after 30 minutes, can be applied only once, and is rejected if the current file differs from the snapshot. An interrupted apply can remain `applying`; API or verification failures become `failed_or_uncertain`. Neither is retried automatically.

Metadata listing uses HA's API and works without a config mount. YAML reads and all proposals/applications require **the same HA instance's configuration directory** at `HA_CONFIG_DIR` (default `/homeassistant`). HAOS already supplies this mount. For Compose, mount the connected HA instance's config directory at `/homeassistant`; a read-only mount is sufficient for blueprint operations because changes go through HA's API. An unrelated or stale copy of the config is not suitable for review or stale-change detection.

Reads are confined to `blueprints/<domain>/...yaml`, reject traversal/symlinks, and are capped at 128 KiB per blueprint. Proposed YAML supports `!input`, including input sections, but rejects `!include`/`!secret` and other custom tags. Local checks validate the basic structure and domain; HA performs blueprint schema/input validation when saving. This is not a runtime test of every possible input or consumer.

Writes use native `blueprint/save` (`allow_override` only for an accepted update) and `blueprint/delete`. The API contract was checked against Home Assistant 2026.11.0. Older releases that reject these operations are reported as failures; there is no unreviewed delete/recreate or direct-file-write fallback. HA can normalize defaults and YAML formatting and remove comments. The server compares saved YAML content with the proposal, ignoring formatting and the default empty input map; an unexpected difference is reported as uncertain and preserves the backup.

Updating can reload all consumers and activate changed automation/script/template behavior. Deleting can break references; current HA rejects blueprints it considers in use, and this server does not bypass that rejection. Proposals include loaded consumer references and inputs; apply rejects changed snapshots and verifies references after updates. Dynamic references and unloaded YAML may be absent; template-blueprint indexing is unavailable and explicitly reported as unknown. Native HA writes have no shared conditional-write transaction with this server: avoid simultaneous UI/file edits, as a narrow check/write race remains. If an update or verification fails, there is no automatic rollback because the API may already have reloaded consumers.

Backups and proposal status persist in `/data/state/blueprint_changes.sqlite3`, table `proposals`, JSON column `payload` (`before`, `after`, `diff`). They are also readable via `blueprint_change_status`. A restoration requires a new proposal and fresh acceptance. Include app data in backups. Update the add-on to **0.4.0**, refresh ChatGPT's plugin tools, and enable `allow_blueprint_writes` only after setting confirmation for `apply_blueprint_change`.

References: [Blueprint schema](https://www.home-assistant.io/docs/blueprint/schema/), [HA blueprint API](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/blueprint/websocket_api.py), [HA save/delete behavior](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/blueprint/models.py).

## Development and validation

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -e ./hassio_mcp
.venv/bin/pytest -q
.venv/bin/ruff check hassio_mcp/src tests scripts
docker build --target compose -t hassio-mcp:0.4.0 hassio_mcp
docker build -t hassio-mcp-haos:0.4.0 hassio_mcp
```

Tests cover actual MCP stdio initialization/discovery, HTTP/WebSocket authentication, read failures, write annotations, proposal expiry/digest/replay/concurrency, stale edits, YAML rollback/path restrictions, and query input boundaries. These use simulated HA responses; a live HAOS/tunnel/ChatGPT acceptance check is still required before enabling writes.

Setup copy lives in `hassio_mcp/src/hassio_mcp/setup_content.py`. After editing it, run `.venv/bin/python scripts/generate_setup_docs.py` to refresh native option translations and the Documentation tab. Tests also cover ingress access restrictions, configuration-help coverage, and starting the setup guide without credentials.

Protocol references: [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), [tunnel client deployment](https://github.com/openai/tunnel-client/blob/main/docs/deployment/docker.md), [HA REST API](https://contributors.home-assistant.io/core/internals/rest-api/), [HA automation config API](https://github.com/home-assistant/core/blob/dev/homeassistant/components/config/automation.py), [HA trace API](https://github.com/home-assistant/core/blob/dev/homeassistant/components/trace/websocket_api.py), [InfluxQL](https://docs.influxdata.com/influxdb/v1/query_language/explore-data/), [InfluxDB v2 query API](https://docs.influxdata.com/influxdb/v2/query-data/execute-queries/influx-api/).

## Dependency inspection

`inspect_dependencies` reads HA’s related-item index for entities, devices, areas, integrations, automations, scripts, scenes, and automation/script blueprints. `blueprint_consumers` adds each loaded consumer’s exact `use_blueprint` path and inputs. Configuration proposals include this impact information. Review it alongside the diff. Failed dependency reads block proposals; an empty or partial index is not proof of no dependents. Blueprint checks reject undefined `!input` references and missing consumer inputs/defaults, including input sections. Conditional input visibility is conservatively treated as requiring a supplied value or default; full HA validation still occurs at save time.

## Reviewed rollback

Use `propose_automation_rollback` or `propose_blueprint_rollback` with the original proposal ID. Each creates a fresh proposal against the current resource, with a new diff, digest, and impact snapshot. It never restores automatically. Review and explicitly accept it, then call the corresponding apply tool. Rollback of a create proposes deletion; rollback of a delete proposes recreation. Automation YAML rollback preserves unrelated entries added or edited since the original change.

## Script and scene configuration

Use `list_resources`, `get_resource_config`, and `propose_resource_change` for script/scene configuration CRUD. Native edits target UI-managed files by config ID (script slug or scene ID). YAML package resources are managed through explicitly allowlisted configuration files. Every modification requires `apply_resource_change` with the exact accepted digest and client confirmation. Enable `allow_management_writes` only after setting all management apply tools to ask every time. `management_change_status` preserves backups and uncertain outcomes in `/data/state/management_changes.sqlite3`; `propose_resource_rollback` prepares a separately reviewed restoration.

## Discovery and diagnostics

`discover_home_assistant` lists entity, device, area and integration metadata or service schemas. Integration data/options/credentials are omitted. Use entity registry `unique_id` to resolve config IDs and registry links to identify devices, areas, and integrations. `diagnose_entities` reports unavailable/unknown/disabled entities and requested IDs without states; `validate_configuration` checks the current HA configuration without modifying or reloading it. Existing logs, trace, history, statistics and dependency tools provide further evidence.
