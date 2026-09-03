"""Stand-in events-service for the self-test: records every POST it receives and answers 201.

The reporting step's contract is the request that leaves the runner, so the self-test asserts on
exactly that rather than on a real Kerno instance it would have to provision and clean up.

Writes one JSON object per request to the path in CAPTURE, defaulting to /tmp/captured.jsonl.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

CAPTURE = os.environ.get("CAPTURE", "/tmp/captured.jsonl")
PORT = int(os.environ.get("PORT", "9099"))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {"unparseable": raw.decode("utf-8", "replace")}

        with open(CAPTURE, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "path": self.path,
                        "key": self.headers.get("x-kerno-virtual-key-id"),
                        "body": body,
                    }
                )
                + "\n"
            )

        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id":"recorded"}')

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
