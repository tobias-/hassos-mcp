# Home Assistant Private MCP

Connect ChatGPT to Home Assistant through an outbound Secure MCP Tunnel.
Read entity values, Recorder/InfluxDB history, automations, and execution traces;
review and approve automation edits in ChatGPT before applying them.

Version 0.13.0 provides offline ESPHome configuration discovery and exact reads with
`esphome_list_devices` and `esphome_get_config`. Comments and `!secret` references
are preserved; inline credentials are rejected without returning the file.
The setup guide explains the optional ESPHome directory
setting and read-only Compose mounts. File presence does not imply that a device
is online. See [ESPHome inspection setup](https://github.com/tobias-/hassos-mcp#esphome-configuration-inspection).

Version 0.16.0 completes the ESPHome extension: live status/logs, toolchain
validation, reviewed source changes with backups, isolated compilation, and
artifact-specific approved OTA. Install/configure the separate **Hassos MCP
ESPHome worker** from this repository for runtime operations. Source changes
and OTA have independent permission switches and require exact acceptance.
Keep both `esphome_apply_config_change` and `esphome_install` on ask every time.
[Worker setup and OTA flow](https://github.com/tobias-/hassos-mcp/blob/master/esphome_worker/README.md).

Version 0.11.0 adds dependency inspection, reviewed rollback, script/scene CRUD,
registry discovery, diagnostics, accepted service/device actions, and allowlisted
configuration-file editing. Refresh the ChatGPT plugin's tools after updating.
Keep every `apply_*` tool on ask every time. The five direct automation controls
are preauthorized; allow those individually in your client if it still prompts.
Additional management writes require `allow_management_writes`; configuration
files require exact entries in `config_files` and the connected HA config mount.

Automation enable/disable/trigger/inspect/reload are preauthorized direct tools,
including when configuration writes are disabled. Create/edit/delete still need
explicit acceptance of a proposal. Update to 0.5.0 and refresh ChatGPT's tool list;
allow direct controls while keeping all apply tools on ask every time.

Blueprints support listing, reading, and proposed create/update/delete changes.
Every modification requires explicit acceptance of the exact diff, followed by
`apply_blueprint_change`. Set ChatGPT to confirm every apply, then enable the
separate `allow_blueprint_writes` setting. Updates can reload all consumers.
Blueprint proposal backups persist in `/data/state/blueprint_changes.sqlite3`.
After updating to 0.4.0, refresh the plugin's tools in ChatGPT.

The LLM can save feature requests and bug reports with `submit_suggestion`.
Ask it to show saved reports with `list_suggestions`. Reports persist in
`/data/state/suggestions.sqlite3` across restarts and updates; include app data
in backups. Refresh the plugin tools in ChatGPT after updating to 0.3.0.

Install this app from the repository's Home Assistant app/add-on store entry.
It builds on your HAOS machine for amd64 or aarch64. It does not publish a network port.

See [DOCS.md](DOCS.md) for setup and
[the repository README](https://github.com/tobias-/hassos-mcp#readme)
for the access model, confirmation requirements, and recovery limitations.
