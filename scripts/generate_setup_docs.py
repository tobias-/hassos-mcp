"""Keep native option descriptions and the Documentation tab aligned with Web UI help."""

import io
import runpy
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "hassio_mcp"


def generated_files():
    content = runpy.run_path(str(APP / "src/hassio_mcp/setup_content.py"))
    configuration = {}
    docs = [
        "# Set up Home Assistant Private MCP",
        "Open **Web UI** on this add-on's Information tab for a guided setup page with clickable links. It works before credentials are configured. Enter values in the **Configuration** tab, then **Save and restart**. The guide never collects or displays your credentials.",
        "**Quick start:** On HAOS, only **Tunnel ID** and **OpenAI runtime API key** need to be filled in. Leave the Home Assistant token blank, InfluxDB URL blank, and automation writes off for your first connection.",
    ]
    for group in content["GROUPS"]:
        docs += ["## " + group["title"], group["intro"]]
        for field in group["fields"]:
            label = field["name"] + (" (required)" if field["required"] else "")
            configuration[field["key"]] = {
                "name": label,
                "description": field["help"]
                + " "
                + field["example"]
                + " Setup links: open Web UI or the Documentation tab. "
                + field["links"][0][1],
            }
            docs += [
                "### " + label,
                "Configuration field: `" + field["key"] + "`",
                field["help"],
                field["example"],
                " · ".join(f"[{label}]({url})" for label, url in field["links"]),
            ]
    docs += [
        "## Save and connect",
        "\n".join(f"{i}. {step}" for i, step in enumerate(content["FINISH"], 1)),
        "## Troubleshooting",
        "| What you see | What to check |\n| --- | --- |\n| Setup guide opens but ChatGPT cannot connect | Enter both required OpenAI values, save, and restart the add-on. An available guide does not mean the tunnel is connected. |\n| Tunnel is missing in ChatGPT | Check that it is associated with your ChatGPT workspace and that the key/user has Tunnels Read + Use. |\n| Permission error from Home Assistant | Leave the HA token blank for automatic access first. If a required API is denied, use an administrator token and the manual URL. |\n| No InfluxDB data | Verify the server version, hostname, database/bucket, read permissions, and existing HA InfluxDB integration. This app reads stored data; it does not start recording it. |\n| Fields still have old names | Refresh the app store, update to 0.2.0 or newer, and reload the configuration page. |\n| Automation writes are disabled | Confirm read access first, configure ChatGPT to ask before each apply, then enable approved edits and restart. |",
        "## Approval and privacy",
        "Keep this tunnel and ChatGPT app private to your personal organization/workspace. The server relies on tunnel access control; it does not independently authenticate ChatGPT user IDs. Run only one instance for a tunnel ID.",
        "An exact proposal ID/digest, expiry, and replay protection bind an apply to a proposed change. They do not prove human approval: ChatGPT's ask-every-time setting is the human confirmation boundary. Saving may activate triggers. Avoid simultaneous edits elsewhere.",
        "Credentials are stored in HAOS app options. Proposal backups live under `/data/state`. Treat HAOS backups as sensitive. If a write fails with uncertain status, inspect the current configuration before proposing again.",
        "[Full access and recovery documentation](https://github.com/tobias-/hassos-mcp#readme)",
    ]
    yaml = YAML()
    yaml.width = 110
    stream = io.StringIO()
    yaml.dump({"configuration": configuration}, stream)
    return {
        APP / "translations/en.yaml": stream.getvalue(),
        APP / "DOCS.md": "\n\n".join(docs) + "\n",
    }


def main():
    for path, text in generated_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
