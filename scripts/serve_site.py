"""Preview the site locally against the live API.

    python -m scripts.serve_site https://dxxxx.cloudfront.net [--port 8080] [--lan]

Builds the site, serves it, and forwards /api/* to the given site so the page shows
the real traveler. Read-only: only GET is forwarded. Listens on this machine only,
unless --lan, which listens on every interface so a phone on the same network can open it.
"""

from __future__ import annotations

import argparse
import http.server
import socket
import ssl
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path
from typing import Any

from scripts.site_build import ROOT, build

PASS_HEADERS = ("content-type", "cache-control", "date", "x-traveler-region")


def _tls() -> ssl.SSLContext:
    try:  # Windows Python has no CA bundle of its own
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Handler(http.server.SimpleHTTPRequestHandler):
    upstream = ""
    tls: ssl.SSLContext

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        if not self.path.startswith("/api/"):
            super().do_GET()
            return
        req = urllib.request.Request(self.upstream + self.path, headers={"user-agent": "traveler-site-preview"})  # noqa: S310
        try:
            resp: Any = urllib.request.urlopen(req, timeout=10, context=self.tls)  # noqa: S310
        except urllib.error.HTTPError as e:
            resp = e
        except OSError as e:
            self.send_error(502, str(e))
            return
        with resp:
            body = resp.read()
            self.send_response(resp.status)
            for name in PASS_HEADERS:
                if resp.headers.get(name):
                    self.send_header(name, resp.headers[name])
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if not self.path.startswith("/api/"):
            super().log_message(format, *args)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("upstream", help="the live site, e.g. https://dxxxx.cloudfront.net")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--lan", action="store_true", help="listen on the local network, not just this machine")
    args = p.parse_args()
    out = ROOT / "build" / "site"
    build(out)
    Handler.upstream = args.upstream.rstrip("/")
    Handler.tls = _tls()
    host = "0.0.0.0" if args.lan else "127.0.0.1"  # noqa: S104 - --lan asks for exactly this
    server = http.server.ThreadingHTTPServer((host, args.port), partial(Handler, directory=str(Path(out))))
    shown = socket.gethostbyname(socket.gethostname()) if args.lan else "127.0.0.1"
    print(f"http://{shown}:{args.port}/  (api -> {Handler.upstream})", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
