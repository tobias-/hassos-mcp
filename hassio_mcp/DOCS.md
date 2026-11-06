# Setup

1. Create a Secure MCP Tunnel associated only with your personal OpenAI organization
   and ChatGPT workspace. Its runtime key needs Tunnels Read + Use.
2. In this app's Configuration tab, enter `tunnel_id` and `openai_api_key`.
   Credentials are stored in HAOS app options, not in the GitHub repository.
3. Leave `ha_token` empty to use the Supervisor's internal Home Assistant API proxy.
   If a required operation is denied by that proxy, configure an administrator's
   Home Assistant long-lived access token and `ha_url: http://homeassistant:8123`.
4. If using InfluxDB, set its add-on hostname and port in `influx_url` and its major
   version in `influx_version`. For v1, configure database/user/password; for v2,
   configure organization/bucket/token. Use credentials with read-only access.
5. Start the app. In ChatGPT, add a custom MCP server using the Tunnel connection
   and select this tunnel. Test `connection_status`, `list_states`, and an automation read.

## Automation changes

Keep writes disabled initially. Set ChatGPT to ask for confirmation on every
`apply_automation_change` call. Then enable `allow_automation_writes` in this app
and restart it. Review the proposed diff and its effects before approving.

The server requires a matching proposal ID and digest, rejects stale or expired
proposals, and refuses replays. Those checks do not prove a human approved;
ChatGPT's confirmation policy provides that boundary. Saving an automation can
activate its triggers. Avoid editing the same automation elsewhere during apply.

UI-managed automations are edited through Home Assistant's configuration API.
For package YAML, add only the necessary relative files to `yaml_files`, such as
`packages/lighting.yaml`. Supported files contain an automation list or a package
with a top-level `automation` list. Existing automations must have unique IDs.
There is no generic service-call or manual automation-trigger tool.

## Private access and storage

This app assumes a single-user Platform organization and private ChatGPT workspace.
The tunnel enforces access; this app does not independently authenticate ChatGPT
user IDs. Do not share its tunnel or ChatGPT app. Run only one instance for a tunnel ID.

HAOS stores credentials in app options and proposal backups under `/data/state`.
Treat app backups as sensitive. A public GitHub repository does not publish the
running service or these credentials.

## Troubleshooting

Check the app logs for missing configuration or tunnel connection errors. Stored
traces/history are limited by Home Assistant retention and exclusions. InfluxDB
queries support v1/v2 with the default Home Assistant tag layout.

After an uncertain write failure, use `automation_change_status` and inspect the
current HA configuration before making a new proposal. Do not blindly retry.

Full installation, recovery, and development documentation:
https://github.com/tobias-/hassio-mcp#readme
