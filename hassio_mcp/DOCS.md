# Set up Home Assistant Private MCP

Open **Web UI** on this add-on's Information tab for a guided setup page with clickable links. It works before credentials are configured. Enter values in the **Configuration** tab, then **Save and restart**. The guide never collects or displays your credentials.

**Quick start:** On HAOS, only **Tunnel ID** and **OpenAI runtime API key** need to be filled in. Leave the Home Assistant token blank, InfluxDB URL blank, and automation writes off for your first connection.

## Connect to ChatGPT

You only need two values to start on HAOS. Get them from the same personal OpenAI organization. Keep the tunnel associated only with your own organization and ChatGPT workspace.

### Tunnel ID (required)

Configuration field: `tunnel_id`

Open OpenAI tunnel settings, create a Secure MCP Tunnel, and copy its tunnel ID here. Associate it with your personal ChatGPT workspace so it appears when you add the app.

Starts with tunnel_ — copy the whole ID, not a URL.

[Open tunnel settings](https://platform.openai.com/settings/organization/tunnels) · [Tunnel setup and permissions](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

### OpenAI runtime API key (required)

Configuration field: `openai_api_key`

In OpenAI API keys, create a dedicated runtime key for this add-on and copy the secret value into this password field. The key's identity needs Tunnels Read + Use. This is the runtime key, not an admin key or a ChatGPT password.

Use the same organization as the tunnel. Save the key when it is shown.

[Open API keys](https://platform.openai.com/settings/organization/api-keys) · [Required tunnel permissions](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#permissions-and-access)

## Home Assistant connection

Leave these settings at their defaults for the HAOS app. It obtains access through Supervisor automatically. Use a manual token only when that connection is denied or when running outside HAOS.

### Home Assistant URL

Configuration field: `ha_url`

Leave http://homeassistant:8123 on HAOS. With a manual token, enter the Home Assistant base address reachable from the add-on, without /api. When the token is blank, the app uses the automatic Supervisor connection instead.

HAOS default: http://homeassistant:8123

[Manual connection instructions](https://github.com/tobias-/hassos-mcp/blob/master/hassio_mcp/DOCS.md#home-assistant-connection)

### Home Assistant access token

Configuration field: `ha_token`

Normally leave blank. For a manual connection, open your Home Assistant user profile → Security → Long-lived access tokens, create a token, and paste it here. Automation editing and traces require an administrator account.

Blank = use HAOS automatic authentication.

[Open Home Assistant profile](https://my.home-assistant.io/redirect/profile/) · [Home Assistant authentication help](https://www.home-assistant.io/docs/authentication/)

## Automation and blueprint changes

Automation enable, disable, trigger, inspect, and reload are directly authorized and work even with configuration writes disabled. Configuration create/edit/delete and blueprint changes require explicit acceptance of each proposal. Configure ChatGPT permissions separately for direct controls and apply tools.

### Allow explicitly accepted blueprint changes

Configuration field: `allow_blueprint_writes`

Keep off until ChatGPT is set to ask every time for apply_blueprint_change. Create, update, and delete each require review of the exact proposal and explicit acceptance. Updates can reload all consumers; deletion can break references. HAOS provides the config mount automatically. Compose needs the connected HA config mounted, which can be read-only for blueprint API writes.

Default: off. Independent of automation editing permission.

[Blueprint review and recovery](https://github.com/tobias-/hassos-mcp#blueprint-crud-and-acceptance) · [Home Assistant blueprint schema](https://www.home-assistant.io/docs/blueprint/schema/)

### Allow accepted management changes

Configuration field: `allow_management_writes`

Enables reviewed scripts/scenes and other management apply tools. Every write requires explicit acceptance of the exact proposal. Keep all management apply tools on ask every time in ChatGPT. The five direct automation operations remain independent of this switch.

Default: off. Configure client confirmation before enabling.

[Management and approval workflow](https://github.com/tobias-/hassos-mcp#access-and-approval-model)

### Allow accepted automation configuration changes

Configuration field: `allow_automation_writes`

Controls configuration create, edit, and delete only. First set ChatGPT to ask every time for apply_automation_change, then enable this and restart. Review the exact diff before accepting. Enable/disable/trigger/inspect/reload use separate direct tools and do not require this switch or a proposal.

Default: off. Direct automation controls remain available.

[Connect the app in ChatGPT](https://developers.openai.com/api/docs/guides/custom-mcp-server) · [How approval and recovery work](https://github.com/tobias-/hassos-mcp#access-and-approval-model)

### Allowed automation YAML files

Configuration field: `yaml_files`

Leave empty for UI-managed automations. To edit package YAML, add the existing file paths relative to your Home Assistant configuration directory. Each target automation needs an id. This grants access only to the listed files.

Example: packages/lighting.yaml

[Home Assistant packages](https://www.home-assistant.io/docs/configuration/packages/) · [Supported YAML layouts](https://github.com/tobias-/hassos-mcp#yaml-editing-and-recovery-limits)

## InfluxDB history

Home Assistant's Recorder history works without InfluxDB settings. Add InfluxDB only for data already stored there. Choose the database server's major version, not the Home Assistant add-on release number.

### InfluxDB server URL

Configuration field: `influx_url`

Open the InfluxDB add-on's Information tab and copy its Hostname. Use http://HOSTNAME:8086, or your configured HTTPS address. This is the database API address, not the browser's ingress URL. Leave blank to skip InfluxDB.

Example: http://a0d7b954-influxdb:8086 — verify your own hostname.

[InfluxDB add-on setup](https://github.com/hassio-addons/app-influxdb/blob/main/influxdb/DOCS.md) · [Find your existing HA InfluxDB settings](https://www.home-assistant.io/integrations/influxdb/)

### InfluxDB major version

Configuration field: `influx_version`

Find the database server version in the InfluxDB UI or its startup logs. Choose 1 for database/username/password, or 2 for organization/bucket/token. This app does not support InfluxDB 3 queries.

1.x: use the three v1 fields. 2.x: use the three v2 fields.

[Compare Home Assistant InfluxDB settings](https://www.home-assistant.io/integrations/influxdb/)

### InfluxDB v1: database

Configuration field: `influx_database`

For version 1 only. Copy the database name from the influxdb section of your Home Assistant configuration or the database list in InfluxDB. Use the existing database containing your recorded values.

Often homeassistant; verify the name in your configuration.

[Locate the v1 database setting](https://www.home-assistant.io/integrations/influxdb/#influxdb-1x-configuration-options)

### InfluxDB v1: read-only username

Configuration field: `influx_user`

For version 1 only. Create a dedicated InfluxDB user and grant READ on your Home Assistant database. Enter that username here. This is an InfluxDB account, not your Home Assistant login.

Suggested account name: hassio_mcp_reader

[Create users and grant database READ access](https://docs.influxdata.com/influxdb/v1/administration/authentication_and_authorization/#user-management-commands)

### InfluxDB v1: password

Configuration field: `influx_password`

For version 1 only. Enter the password you chose for the read-only InfluxDB user. If you no longer have it, set a new password for that dedicated account in InfluxDB.

Use the password for the username above.

[Manage InfluxDB user credentials](https://docs.influxdata.com/influxdb/v1/administration/authentication_and_authorization/#user-management-commands)

### InfluxDB v2: organization

Configuration field: `influx_org`

For version 2 only. In the InfluxDB UI, open the account menu → Switch Organizations and copy the organization name that owns your Home Assistant bucket.

This is your InfluxDB organization, independent of OpenAI.

[Find your InfluxDB organization](https://docs.influxdata.com/influxdb/v2/admin/organizations/view-orgs/)

### InfluxDB v2: bucket

Configuration field: `influx_bucket`

For version 2 only. Open InfluxDB → Load Data → Buckets and copy the name of the bucket receiving Home Assistant data. It should match the bucket in your HA influxdb configuration.

Enter the existing bucket name, not its ID.

[Find your bucket](https://docs.influxdata.com/influxdb/v2/admin/buckets/view-buckets/)

### InfluxDB v2: read-only API token

Configuration field: `influx_token`

For version 2 only. In InfluxDB, open Load Data → API Tokens → Generate API Token → Custom API Token. Grant Read for your Home Assistant bucket, then copy the token here. Leave the v1 username and password blank.

Grant bucket Read access; this app does not need Write access.

[Create a scoped InfluxDB API token](https://docs.influxdata.com/influxdb/v2/admin/tokens/create-token/)

## Save and connect

1. Enter the values in this add-on's Configuration tab, then Save and restart the add-on. This guide does not collect or display your credentials.
2. Open ChatGPT → Plugins → Add custom MCP server. Choose Connection: Tunnel and select the tunnel you created.
3. Ask ChatGPT to run connection_status and list a few Home Assistant states. Keep automation writes off until those reads work.

## Troubleshooting

| What you see | What to check |
| --- | --- |
| Setup guide opens but ChatGPT cannot connect | Enter both required OpenAI values, save, and restart the add-on. An available guide does not mean the tunnel is connected. |
| Tunnel is missing in ChatGPT | Check that it is associated with your ChatGPT workspace and that the key/user has Tunnels Read + Use. |
| Permission error from Home Assistant | Leave the HA token blank for automatic access first. If a required API is denied, use an administrator token and the manual URL. |
| No InfluxDB data | Verify the server version, hostname, database/bucket, read permissions, and existing HA InfluxDB integration. This app reads stored data; it does not start recording it. |
| Fields still have old names | Refresh the app store, update to 0.2.0 or newer, and reload the configuration page. |
| Automation writes are disabled | Confirm read access first, configure ChatGPT to ask before each apply, then enable approved edits and restart. |

## Approval and privacy

Keep this tunnel and ChatGPT app private to your personal organization/workspace. The server relies on tunnel access control; it does not independently authenticate ChatGPT user IDs. Run only one instance for a tunnel ID.

An exact proposal ID/digest, expiry, and replay protection bind an apply to a proposed change. They do not prove human approval: ChatGPT's ask-every-time setting is the human confirmation boundary. Saving may activate triggers. Avoid simultaneous edits elsewhere.

Credentials are stored in HAOS app options. Proposal backups live under `/data/state`. Treat HAOS backups as sensitive. If a write fails with uncertain status, inspect the current configuration before proposing again.

[Full access and recovery documentation](https://github.com/tobias-/hassos-mcp#readme)
