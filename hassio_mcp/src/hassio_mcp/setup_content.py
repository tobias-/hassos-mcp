"""Shared, public setup guidance for the HA configuration form, Web UI, and docs."""

TUNNEL_GUIDE = "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
TUNNELS = "https://platform.openai.com/settings/organization/tunnels"
KEYS = "https://platform.openai.com/settings/organization/api-keys"
HA_AUTH = "https://www.home-assistant.io/docs/authentication/"
INFLUX_HA = "https://www.home-assistant.io/integrations/influxdb/"
INFLUX_ADDON = (
    "https://github.com/hassio-addons/app-influxdb/blob/main/influxdb/DOCS.md"
)
INFLUX_V1 = "https://docs.influxdata.com/influxdb/v1/administration/authentication_and_authorization/"
INFLUX_TOKEN = "https://docs.influxdata.com/influxdb/v2/admin/tokens/create-token/"
DOCS = "https://github.com/tobias-/hassos-mcp/blob/master/hassio_mcp/DOCS.md"

GROUPS = [
    {
        "id": "openai",
        "title": "Connect to ChatGPT",
        "badge": "Start here",
        "intro": "You only need two values to start on HAOS. Get them from the same personal OpenAI organization. Keep the tunnel associated only with your own organization and ChatGPT workspace.",
        "fields": [
            {
                "key": "tunnel_id",
                "name": "Tunnel ID",
                "required": True,
                "help": "Open OpenAI tunnel settings, create a Secure MCP Tunnel, and copy its tunnel ID here. Associate it with your personal ChatGPT workspace so it appears when you add the app.",
                "example": "Starts with tunnel_ — copy the whole ID, not a URL.",
                "links": [
                    ("Open tunnel settings", TUNNELS),
                    ("Tunnel setup and permissions", TUNNEL_GUIDE),
                ],
            },
            {
                "key": "openai_api_key",
                "name": "OpenAI runtime API key",
                "required": True,
                "help": "In OpenAI API keys, create a dedicated runtime key for this add-on and copy the secret value into this password field. The key's identity needs Tunnels Read + Use. This is the runtime key, not an admin key or a ChatGPT password.",
                "example": "Use the same organization as the tunnel. Save the key when it is shown.",
                "links": [
                    ("Open API keys", KEYS),
                    (
                        "Required tunnel permissions",
                        TUNNEL_GUIDE + "#permissions-and-access",
                    ),
                ],
            },
        ],
    },
    {
        "id": "home-assistant",
        "title": "Home Assistant connection",
        "badge": "Automatic on HAOS",
        "intro": "Leave these settings at their defaults for the HAOS app. It obtains access through Supervisor automatically. Use a manual token only when that connection is denied or when running outside HAOS.",
        "fields": [
            {
                "key": "ha_url",
                "name": "Home Assistant URL",
                "required": False,
                "help": "Leave http://homeassistant:8123 on HAOS. With a manual token, enter the Home Assistant base address reachable from the add-on, without /api. When the token is blank, the app uses the automatic Supervisor connection instead.",
                "example": "HAOS default: http://homeassistant:8123",
                "links": [
                    (
                        "Manual connection instructions",
                        DOCS + "#home-assistant-connection",
                    )
                ],
            },
            {
                "key": "ha_token",
                "name": "Home Assistant access token",
                "required": False,
                "help": "Normally leave blank. For a manual connection, open your Home Assistant user profile → Security → Long-lived access tokens, create a token, and paste it here. Automation editing and traces require an administrator account.",
                "example": "Blank = use HAOS automatic authentication.",
                "links": [
                    (
                        "Open Home Assistant profile",
                        "https://my.home-assistant.io/redirect/profile/",
                    ),
                    ("Home Assistant authentication help", HA_AUTH),
                ],
            },
        ],
    },
    {
        "id": "automations",
        "title": "Automation and blueprint changes",
        "badge": "Optional",
        "intro": "Automation enable, disable, trigger, inspect, and reload are directly authorized and work even with configuration writes disabled. Configuration create/edit/delete and blueprint changes require explicit acceptance of each proposal. Configure ChatGPT permissions separately for direct controls and apply tools.",
        "fields": [
            {
                "key": "allow_blueprint_writes",
                "name": "Allow explicitly accepted blueprint changes",
                "required": False,
                "help": "Keep off until ChatGPT is set to ask every time for apply_blueprint_change. Create, update, and delete each require review of the exact proposal and explicit acceptance. Updates can reload all consumers; deletion can break references. HAOS provides the config mount automatically. Compose needs the connected HA config mounted, which can be read-only for blueprint API writes.",
                "example": "Default: off. Independent of automation editing permission.",
                "links": [
                    (
                        "Blueprint review and recovery",
                        "https://github.com/tobias-/hassos-mcp#blueprint-crud-and-acceptance",
                    ),
                    (
                        "Home Assistant blueprint schema",
                        "https://www.home-assistant.io/docs/blueprint/schema/",
                    ),
                ],
            },
            {
                "key": "allow_management_writes",
                "name": "Allow accepted management changes",
                "required": False,
                "help": "Enables reviewed scripts/scenes and other management apply tools. Every write requires explicit acceptance of the exact proposal. Keep all management apply tools on ask every time in ChatGPT. The five direct automation operations remain independent of this switch.",
                "example": "Default: off. Configure client confirmation before enabling.",
                "links": [
                    (
                        "Management and approval workflow",
                        "https://github.com/tobias-/hassos-mcp#access-and-approval-model",
                    )
                ],
            },
            {
                "key": "allow_automation_writes",
                "name": "Allow accepted automation configuration changes",
                "required": False,
                "help": "Controls configuration create, edit, and delete only. First set ChatGPT to ask every time for apply_automation_change, then enable this and restart. Review the exact diff before accepting. Enable/disable/trigger/inspect/reload use separate direct tools and do not require this switch or a proposal.",
                "example": "Default: off. Direct automation controls remain available.",
                "links": [
                    (
                        "Connect the app in ChatGPT",
                        "https://developers.openai.com/api/docs/guides/custom-mcp-server",
                    ),
                    (
                        "How approval and recovery work",
                        "https://github.com/tobias-/hassos-mcp#access-and-approval-model",
                    ),
                ],
            },
            {
                "key": "yaml_files",
                "name": "Allowed automation YAML files",
                "required": False,
                "help": "Leave empty for UI-managed automations. To edit package YAML, add the existing file paths relative to your Home Assistant configuration directory. Each target automation needs an id. This grants access only to the listed files.",
                "example": "Example: packages/lighting.yaml",
                "links": [
                    (
                        "Home Assistant packages",
                        "https://www.home-assistant.io/docs/configuration/packages/",
                    ),
                    (
                        "Supported YAML layouts",
                        "https://github.com/tobias-/hassos-mcp#yaml-editing-and-recovery-limits",
                    ),
                ],
            },
            {
                "key": "config_files",
                "name": "Allowed configuration files",
                "required": False,
                "help": "List exact YAML paths relative to the connected Home Assistant config directory, for example packages/lights.yaml or scripts.yaml. This grants read and reviewed whole-file editing access. Secret files, hidden paths, symlinks, blueprint files, and inline credentials are excluded. Use native automation/script/scene APIs where possible. Configuration writes also require accepted proposals and allow_management_writes.",
                "example": "Default: empty. Whole-file rollback can replace later edits; review the new diff.",
                "links": [
                    (
                        "Restricted configuration editing",
                        "https://github.com/tobias-/hassos-mcp#restricted-configuration-files",
                    )
                ],
            },
        ],
    },
    {
        "id": "esphome",
        "title": "ESPHome configuration discovery",
        "badge": "Read-only",
        "intro": "Find stored device configurations even when the hardware is offline. This only inspects local YAML; it does not check device connectivity or run ESPHome Builder.",
        "fields": [
            {
                "key": "esphome_worker_url",
                "name": "ESPHome worker URL",
                "required": False,
                "help": "Install the Hassos MCP ESPHome worker add-on from this repository. Copy its Hostname from Information and enter http://HOSTNAME:6053. This is a separate pinned ESPHome runtime, not Builder's UI URL. Compose: enable the esphome profile and use http://esphome-worker:6053. The worker needs LAN access to devices and internet for compiler dependencies; no HA config mount or Docker socket is used.",
                "example": "Blank disables runtime operations; source discovery/reading still work.",
                "links": [
                    (
                        "Worker setup",
                        "https://github.com/tobias-/hassos-mcp/blob/master/esphome_worker/README.md",
                    )
                ],
            },
            {
                "key": "esphome_worker_token",
                "name": "ESPHome worker authentication token",
                "required": False,
                "help": "Generate a new random token with openssl rand -hex 32 and save the same value in this field and the worker add-on's worker_token field. Compose uses ESPHOME_WORKER_TOKEN in .env.local. Keep the worker on a trusted private network; use HTTPS when crossing an untrusted network. Never use a Home Assistant or OpenAI API key here.",
                "example": "At least 32 characters. Do not paste it into chat or commit it.",
                "links": [
                    (
                        "Worker authentication and isolation",
                        "https://github.com/tobias-/hassos-mcp/blob/master/esphome_worker/README.md",
                    )
                ],
            },
            {
                "key": "esphome_config_dir",
                "name": "ESPHome configuration directory",
                "required": False,
                "help": "Normally leave blank on HAOS: the app uses the esphome subdirectory of its Home Assistant configuration mount (/homeassistant/esphome in this app). In ESPHome Builder, check the YAML filenames and its startup log for the configuration directory. A /config/esphome path inside Builder refers to the shared HA config; use /homeassistant/esphome here. For another layout, enter the absolute directory as mounted inside this MCP container. Setting a path does not create a mount or grant access to another app's private storage. Compose users must mount the directory read-only and set ESPHOME_CONFIG_DIR to that container path.",
                "example": "Blank = HA_CONFIG_DIR/esphome. Compose example: /esphome-config. Discovery never implies that a device is online.",
                "links": [
                    (
                        "ESPHome mount and recovery setup",
                        "https://github.com/tobias-/hassos-mcp#esphome-configuration-inspection",
                    ),
                    (
                        "ESPHome Builder configuration",
                        "https://github.com/esphome/home-assistant-addon/blob/main/esphome/config.yaml",
                    ),
                    (
                        "Home Assistant mount paths",
                        "https://contributors.home-assistant.io/apps/reference/configuration/#folders",
                    ),
                    ("ESPHome YAML and secrets", "https://esphome.io/guides/yaml/"),
                ],
            },
        ],
    },
    {
        "id": "influx",
        "title": "InfluxDB history",
        "badge": "Optional",
        "intro": "Home Assistant's Recorder history works without InfluxDB settings. Add InfluxDB only for data already stored there. Choose the database server's major version, not the Home Assistant add-on release number.",
        "fields": [
            {
                "key": "influx_url",
                "name": "InfluxDB server URL",
                "required": False,
                "help": "Open the InfluxDB add-on's Information tab and copy its Hostname. Use http://HOSTNAME:8086, or your configured HTTPS address. This is the database API address, not the browser's ingress URL. Leave blank to skip InfluxDB.",
                "example": "Example: http://a0d7b954-influxdb:8086 — verify your own hostname.",
                "links": [
                    ("InfluxDB add-on setup", INFLUX_ADDON),
                    ("Find your existing HA InfluxDB settings", INFLUX_HA),
                ],
            },
            {
                "key": "influx_version",
                "name": "InfluxDB major version",
                "required": False,
                "help": "Find the database server version in the InfluxDB UI or its startup logs. Choose 1 for database/username/password, or 2 for organization/bucket/token. This app does not support InfluxDB 3 queries.",
                "example": "1.x: use the three v1 fields. 2.x: use the three v2 fields.",
                "links": [("Compare Home Assistant InfluxDB settings", INFLUX_HA)],
            },
            {
                "key": "influx_database",
                "name": "InfluxDB v1: database",
                "required": False,
                "help": "For version 1 only. Copy the database name from the influxdb section of your Home Assistant configuration or the database list in InfluxDB. Use the existing database containing your recorded values.",
                "example": "Often homeassistant; verify the name in your configuration.",
                "links": [
                    (
                        "Locate the v1 database setting",
                        INFLUX_HA + "#influxdb-1x-configuration-options",
                    )
                ],
            },
            {
                "key": "influx_user",
                "name": "InfluxDB v1: read-only username",
                "required": False,
                "help": "For version 1 only. Create a dedicated InfluxDB user and grant READ on your Home Assistant database. Enter that username here. This is an InfluxDB account, not your Home Assistant login.",
                "example": "Suggested account name: hassio_mcp_reader",
                "links": [
                    (
                        "Create users and grant database READ access",
                        INFLUX_V1 + "#user-management-commands",
                    )
                ],
            },
            {
                "key": "influx_password",
                "name": "InfluxDB v1: password",
                "required": False,
                "help": "For version 1 only. Enter the password you chose for the read-only InfluxDB user. If you no longer have it, set a new password for that dedicated account in InfluxDB.",
                "example": "Use the password for the username above.",
                "links": [
                    (
                        "Manage InfluxDB user credentials",
                        INFLUX_V1 + "#user-management-commands",
                    )
                ],
            },
            {
                "key": "influx_org",
                "name": "InfluxDB v2: organization",
                "required": False,
                "help": "For version 2 only. In the InfluxDB UI, open the account menu → Switch Organizations and copy the organization name that owns your Home Assistant bucket.",
                "example": "This is your InfluxDB organization, independent of OpenAI.",
                "links": [
                    (
                        "Find your InfluxDB organization",
                        "https://docs.influxdata.com/influxdb/v2/admin/organizations/view-orgs/",
                    )
                ],
            },
            {
                "key": "influx_bucket",
                "name": "InfluxDB v2: bucket",
                "required": False,
                "help": "For version 2 only. Open InfluxDB → Load Data → Buckets and copy the name of the bucket receiving Home Assistant data. It should match the bucket in your HA influxdb configuration.",
                "example": "Enter the existing bucket name, not its ID.",
                "links": [
                    (
                        "Find your bucket",
                        "https://docs.influxdata.com/influxdb/v2/admin/buckets/view-buckets/",
                    )
                ],
            },
            {
                "key": "influx_token",
                "name": "InfluxDB v2: read-only API token",
                "required": False,
                "help": "For version 2 only. In InfluxDB, open Load Data → API Tokens → Generate API Token → Custom API Token. Grant Read for your Home Assistant bucket, then copy the token here. Leave the v1 username and password blank.",
                "example": "Grant bucket Read access; this app does not need Write access.",
                "links": [("Create a scoped InfluxDB API token", INFLUX_TOKEN)],
            },
        ],
    },
]

FINISH = [
    "Enter the values in this add-on's Configuration tab, then Save and restart the add-on. This guide does not collect or display your credentials.",
    "Open ChatGPT → Plugins → Add custom MCP server. Choose Connection: Tunnel and select the tunnel you created.",
    "Ask ChatGPT to run connection_status and list a few Home Assistant states. Keep automation writes off until those reads work.",
]
