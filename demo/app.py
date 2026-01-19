# app.py
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

START_DELAY = int(os.environ.get("STARTUP_DELAY_SECONDS", "30"))
START_TIME = time.time()
BUILD_TIME = os.environ.get("BUILD_TIME", "unknown")
server = None

# Log build time on startup
print(f"[APP] Build time: {BUILD_TIME}", flush=True)
print(f"[APP] Starting server on port {os.environ.get('PORT', '8080')}", flush=True)

def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    print(f"\n[APP] Received signal {sig}, shutting down gracefully...", flush=True)
    if server:
        server.shutdown()
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

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
                self._write(200, f"ready (build: {BUILD_TIME})\n")
            else:
                self._write(503, f"starting (build: {BUILD_TIME})\n")
        elif self.path == "/version":
            self._write(200, f"build-time: {BUILD_TIME}\n")
        else:
            self._write(404, "not found\n")

def main():
    global server
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("", port), Handler)
    print(f"[APP] Server ready, listening on port {port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[APP] Interrupted, shutting down...", flush=True)
        server.shutdown()

if __name__ == "__main__":
    main()
