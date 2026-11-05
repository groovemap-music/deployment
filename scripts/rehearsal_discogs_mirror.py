#!/usr/bin/env python3
"""A local stand-in for data.discogs.com, for scripts/rehearse_catalog_reset.py only.

The extractor's download path has no base-URL setting, so the rehearsal gives this server the name
data.discogs.com on the stack's network and points SSL_CERT_FILE at a throwaway CA. That runs the
released extractor's real listing, checksum and reuse logic against a small dump, and each request
is logged, which is how the rehearsal proves a forced run downloads no dump file.

Arguments: DUMP_DIRECTORY CERTIFICATE KEY. Standard library only.
"""

from __future__ import annotations

import http.server
import json
import ssl
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class Mirror(http.server.BaseHTTPRequestHandler):
    directory = Path()

    def log_message(self, format: str, *args: object) -> None:  # noqa: ARG002 - the base class's signature
        return

    def send(self, body: bytes, content_type: str = "text/html") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        print(json.dumps({"path": self.path}), flush=True)
        if "download" in query:
            name = Path(query["download"][0]).name
            path = self.directory / name
            if not path.is_file():
                self.send_error(404)
                return
            self.send(path.read_bytes(), "application/octet-stream")
            return
        if "prefix" in query:
            links = [f'<a href="?download=data%2F2026%2F{path.name}">{path.name}</a>' for path in sorted(self.directory.iterdir())]
            self.send("\n".join(links).encode())
            return
        self.send(b'<a href="?prefix=data%2F2026%2F">2026/</a>')


def main() -> None:
    Mirror.directory = Path(sys.argv[1])
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 443), Mirror)  # noqa: S104 - only on the disposable stack's network
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(sys.argv[2], sys.argv[3])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
