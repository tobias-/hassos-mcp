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
