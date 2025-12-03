# app.py
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

START_DELAY = int(os.environ.get("STARTUP_DELAY_SECONDS", "30"))
START_TIME = time.time()

class Handler(BaseHTTPRequestHandler):
    def _write(self, code: int, body: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        now = time.time()
        if self.path == "/livez":
            self._write(200, "ok\n")
        elif self.path == "/readyz":
            if now - START_TIME >= START_DELAY:
                self._write(200, "ready\n")
            else:
                self._write(503, "starting\n")
        else:
            self._write(404, "not found\n")

def main():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("", port), Handler)
    server.serve_forever()

if __name__ == "__main__":
    main()
