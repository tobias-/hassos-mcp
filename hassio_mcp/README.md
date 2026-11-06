# Home Assistant Private MCP

Connect ChatGPT to Home Assistant through an outbound Secure MCP Tunnel.
Read entity values, Recorder/InfluxDB history, automations, and execution traces;
review and approve automation edits in ChatGPT before applying them.

The LLM can save feature requests and bug reports with `submit_suggestion`.
Ask it to show saved reports with `list_suggestions`. Reports persist in
`/data/state/suggestions.sqlite3` across restarts and updates; include app data
in backups. Refresh the plugin tools in ChatGPT after updating to 0.3.0.

Install this app from the repository's Home Assistant app/add-on store entry.
It builds on your HAOS machine for amd64 or aarch64. It does not publish a network port.

See [DOCS.md](DOCS.md) for setup and
[the repository README](https://github.com/tobias-/hassos-mcp#readme)
for the access model, confirmation requirements, and recovery limitations.
