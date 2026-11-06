# 0.11.2

- Fix blueprint consumer inspection when Home Assistant returns expanded runtime configuration without the original blueprint reference.
- Resolve original inputs through saved automation/script configuration IDs, including renamed script entities, with allowlisted automation YAML fallback.
- Keep stale-reference and changed-input protections; explain unavailable source configuration instead of incorrectly reporting a changed dependency index.

# 0.11.1

- Add a read-only get_version tool reporting the running server's installed release version without contacting Home Assistant.

# 0.11.0

- Add explicitly allowlisted configuration-file read/edit/create/delete with accepted diffs and persistent backups.
- Validate YAML locally and full HA config after accepted writes; restore rejected files before reload when untouched externally.
- Add separately accepted whole-file rollback and reviewed optional component reload.
- Exclude secret/hidden/symlink paths and inline credential-bearing management payloads.

# 0.10.0

- Add explicitly accepted service/action proposals for devices, script execution, scene activation, reloads and restart.
- Bind approval to exact arguments, registered schema, explicit target identities and dependency snapshots.
- Reject broadened targets and consumed/uncertain retries; physical effects have no automatic rollback.

# 0.9.0

- Discover entity/device/area/integration metadata and supported service schemas.
- Add availability diagnostics and read-only full configuration validation.
- Exclude config-entry data/options/credentials from discovery responses.

# 0.8.0

- Add reviewed native script/scene configuration CRUD with dependency checks, verification and rollback proposals.
- Add a separate management write switch and persistent management proposal status.

# 0.7.0

- Add explicit rollback proposals for blueprint and automation backups.
- Require fresh acceptance and current-state checks for every restoration.
- Restore only the selected YAML automation while preserving unrelated later edits.

# 0.6.0

- Add dependency inspection and blueprint consumer/input snapshots to proposals.
- Reject changed dependency snapshots and missing blueprint inputs before writes.
- Verify consumer references survive blueprint updates; explicitly report indexing limits.

# 0.5.0

- Add direct automation enable, disable, trigger, inspect, and reload tools without a proposal or additional acceptance step.
- Keep configuration create/update/delete and all blueprint writes behind explicit acceptance.
- Add reviewed automation deletion through the UI API or an allowlisted YAML list.
- Support adding new automations to existing allowlisted YAML files.
- Add automation discovery and combined state/configuration/trace inspection.

# 0.4.0

- Add blueprint list/read and reviewed create/update/delete for automation, script, and template domains.
- Require explicit acceptance before every apply; add a separate disabled-by-default blueprint write setting.
- Persist exact diffs and before/after backups, with expiry, stale-file detection, and one-time application.
- Use HA's native save/delete APIs; report failures as uncertain without unsafe retries or fallback writes.
- Add linked setup guidance and verification for blueprint permissions and recovery.

# 0.3.0

- Add a persistent suggestion box for LLM feature requests and observed bugs.
- Add submit_suggestion and paginated list_suggestions tools, usable with automation writes disabled and Home Assistant offline.
- Retain reports in private SQLite storage across restarts and updates, deduplicate exact repeats, and redact known configured credentials.

# 0.2.0

- Add a setup Web UI with direct links and step-by-step guidance for every setting.
- Keep the guide available before tunnel credentials have been configured.
- Add readable configuration labels and help text to the native HAOS form.
- Explain required settings, automatic HAOS access, and optional InfluxDB v1/v2 values.
- Expand the Documentation tab with matching links and troubleshooting.

# 0.1.0

- Initial private MCP server with outbound OpenAI Secure MCP Tunnel.
- Current state, Recorder history/statistics, and InfluxDB v1/v2 reads.
- Automation configuration, execution traces, and error-log inspection.
- Reviewed automation proposals with expiry, replay rejection, backups, and YAML validation rollback.
- GitHub-backed HAOS installation and Docker Compose fallback.
