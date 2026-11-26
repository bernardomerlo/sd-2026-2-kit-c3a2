"""Inference falso (so stdlib) para testar o worker isolado."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"status": "ok"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/predict":
            self._send(200, {"prediction": "mock", "documents": [],
                             "latency_ms": 1, "echo_len": len(str(body))})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", 8001), H).serve_forever()
