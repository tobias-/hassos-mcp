"""Credential-free setup guide served exclusively through HA Supervisor ingress."""

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from .setup_content import DOCS, FINISH, GROUPS

INGRESS_PROXY = "172.30.32.2"
STYLE = """
:root{color-scheme:light dark;--bg:#f4f7fb;--card:#fff;--text:#182638;--muted:#536578;--line:#dce4ed;--accent:#086e96;--tint:#e8f4fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 system-ui,-apple-system,sans-serif}
main{max-width:1050px;margin:auto;padding:40px 24px 72px}header{padding-bottom:28px}.eyebrow{color:var(--accent);font-weight:700;letter-spacing:.12em;font-size:12px;text-transform:uppercase}
h1{font-size:clamp(30px,5vw,46px);line-height:1.15;margin:12px 0 16px;letter-spacing:-.035em}h2{font-size:24px;line-height:1.3;margin:0}h3{font-size:18px;line-height:1.4;margin:0 0 6px}
p{margin:10px 0;color:var(--muted)}.lead{font-size:18px;max-width:720px}.start{background:var(--tint);border-left:4px solid var(--accent);padding:16px 20px;border-radius:8px;margin:22px 0}
nav{display:flex;flex-wrap:wrap;gap:8px;margin:24px 0}a{color:var(--accent);text-underline-offset:3px}a:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:4px}
nav a,.link{display:inline-block;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);text-decoration:none;font-size:14px;font-weight:600}.link:hover,nav a:hover{background:var(--tint)}
section{margin:28px 0;scroll-margin-top:20px}.section-head{display:flex;align-items:center;flex-wrap:wrap;gap:12px}.badge{font-size:12px;line-height:1.5;padding:4px 9px;border-radius:20px;background:var(--tint);color:var(--accent);font-weight:700}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:18px}.field{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px;overflow-wrap:anywhere}
.key{font:12px ui-monospace,monospace;color:var(--muted)}.example{background:var(--bg);border-radius:6px;padding:10px 12px;font-size:14px;margin:16px 0}.links{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
details{border:1px solid var(--line);border-radius:12px;padding:18px 22px;margin-top:14px;background:var(--card)}summary{cursor:pointer;font-weight:650}.steps{padding-left:24px}.steps li{padding:8px 0}footer{border-top:1px solid var(--line);padding-top:24px;color:var(--muted);font-size:14px}
@media(max-width:650px){main{padding:24px 16px 48px}.grid{grid-template-columns:1fr}.field{padding:18px}}
@media(prefers-color-scheme:dark){:root{--bg:#101923;--card:#182432;--text:#edf4fb;--muted:#b3c3d4;--line:#344454;--accent:#77cef1;--tint:#203b4a}}
"""


def render_page():
    parts = [
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Home Assistant Private MCP · Setup</title><style>{STYLE}</style></head><body><main>',
        '<header><div class="eyebrow">Home Assistant Private MCP</div><h1>Connect your home to ChatGPT</h1><p class="lead">Find each setting, follow its setup link, then enter the value in the add-on’s Configuration tab.</p><div class="start"><strong>Start with just two values</strong><br>Tunnel ID and OpenAI runtime API key. Home Assistant connects automatically on HAOS. InfluxDB and automation editing can wait.</div></header>',
        '<nav aria-label="Setup sections">',
    ]
    for group in GROUPS:
        parts.append(f'<a href="#{group["id"]}">{escape(group["title"])}</a>')
    parts.append('<a href="#finish">Save & connect</a></nav>')
    for group in GROUPS:
        parts.append(
            f'<section id="{group["id"]}"><div class="section-head"><h2>{escape(group["title"])}</h2><span class="badge">{escape(group["badge"])}</span></div><p>{escape(group["intro"])}</p>'
        )
        if group["id"] != "openai":
            parts.append(
                "<details><summary>Show settings and where to find them</summary>"
            )
        parts.append('<div class="grid">')
        for field in group["fields"]:
            parts.append(
                f'<article class="field" id="{field["key"]}"><h3>{escape(field["name"])}</h3><span class="key">{field["key"]}</span><p>{escape(field["help"])}</p><div class="example">{escape(field["example"])}</div><div class="links">'
            )
            for label, url in field["links"]:
                parts.append(
                    f'<a class="link" href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(label)} ↗</a>'
                )
            parts.append("</div></article>")
        parts.append("</div>")
        if group["id"] != "openai":
            parts.append("</details>")
        parts.append("</section>")
    parts.append(
        '<section id="finish"><h2>Save, restart, connect</h2><ol class="steps">'
    )
    parts.extend(f"<li>{escape(step)}</li>" for step in FINISH)
    parts.append(
        f'</ol></section><footer>Links open in a new tab. <a href="{DOCS}" target="_blank" rel="noopener noreferrer">Full setup and troubleshooting guide ↗</a></footer></main></body></html>'
    )
    return "".join(parts).encode()


class SetupHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Per HA ingress requirements, trust the TCP peer, never a forwarded header.
        if self.client_address[0] != INGRESS_PROXY:
            self.send_error(403)
            return
        if self.path.split("?", 1)[0] not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = render_page()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def start_setup_server():
    server = ThreadingHTTPServer(("0.0.0.0", 8099), SetupHandler)
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True, name="setup-guide").start()
    return server
