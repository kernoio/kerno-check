#!/usr/bin/env python3
"""Stands in for events-service in self-test: records every request as one JSON line and answers 200."""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

CAPTURE = sys.argv[2]
RESPONSE = json.dumps(
    {
        "snapshot": {"gitBranch": "main", "commitSha": "0000000000", "endpoints": []},
        "previous": None,
        "changes": {"added": [], "removed": [], "changed": []},
    }
).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def _record(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        with open(CAPTURE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"method": self.command, "path": self.path, "body": body}) + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(RESPONSE)))
        self.end_headers()
        self.wfile.write(RESPONSE)

    do_GET = do_POST = do_PUT = do_PATCH = _record

    def log_message(self, *_: object) -> None:
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
