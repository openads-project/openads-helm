#!/usr/bin/env python3
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


STATE_KEYS = [
    "IFACE", "AUTO_PEER", "PEER_HOST", "PEER_PORT", "ZENOH_PORT",
    "DELAY", "JITTER", "LOSS", "RATE", "DIST", "LIMIT",
]
KEY_MAP = {
    "iface": "IFACE",
    "autoPeer": "AUTO_PEER",
    "peerHost": "PEER_HOST",
    "peerPort": "PEER_PORT",
    "zenohPort": "ZENOH_PORT",
    "delay": "DELAY",
    "jitter": "JITTER",
    "loss": "LOSS",
    "rate": "RATE",
    "distribution": "DIST",
    "limit": "LIMIT",
}

state_lock = threading.Lock()
state = {key: os.getenv(key, "") for key in STATE_KEYS}
last_applied_at = None
last_error = None


def _is_true(value):
    return str(value).lower() in ("1", "true", "yes", "on")


def _validate(cfg):
    if not cfg.get("IFACE"):
        return "iface must be set"
    if _is_true(cfg.get("AUTO_PEER", "true")) and not cfg.get("PEER_HOST"):
        return "peerHost must be set when autoPeer=true"
    if not cfg.get("PEER_HOST") and not cfg.get("PEER_PORT"):
        return "either peerHost or peerPort must be set"
    if not re.match(r"^[0-9]+$", cfg["ZENOH_PORT"]):
        return "zenohPort must be an integer"
    if not re.match(r"^[0-9]+(\.[0-9]+)?ms$", cfg["DELAY"]):
        return "delay must look like 30ms"
    if not re.match(r"^[0-9]+(\.[0-9]+)?ms$", cfg["JITTER"]):
        return "jitter must look like 5ms"
    if not re.match(r"^[0-9]+(\.[0-9]+)?%$", cfg["LOSS"]):
        return "loss must look like 0.5%"
    if not re.match(r"^[0-9]+(\.[0-9]+)?(kbit|mbit|gbit)$", cfg["RATE"]):
        return "rate must look like 50mbit"
    if cfg.get("LIMIT") and not re.match(r"^[0-9]+$", cfg["LIMIT"]):
        return "limit must be an integer"
    return None


def _apply(cfg):
    env = os.environ.copy()
    env.update({key: str(cfg[key]) for key in STATE_KEYS})
    proc = subprocess.run(
        ["python3", "/netem/apply.py"], capture_output=True, text=True, env=env
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "") + (proc.stdout or ""))
    return (proc.stdout or "").strip()


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("netem-http: " + fmt % args, flush=True)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
            return
        if self.path == "/v1/netem/state":
            with state_lock:
                self._json(200, {
                    "state": state,
                    "lastAppliedAt": last_applied_at,
                    "lastError": last_error,
                })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        global last_applied_at
        global last_error

        if self.path != "/v1/netem/apply":
            self._json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json(400, {"error": "invalid json"})
            return

        updates = {}
        for key, value in payload.items():
            if key in KEY_MAP:
                updates[KEY_MAP[key]] = "" if value is None else str(value)

        with state_lock:
            candidate = dict(state)
            candidate.update(updates)

            validation_error = _validate(candidate)
            if validation_error:
                self._json(400, {"error": validation_error})
                return

            if bool(payload.get("dryRun", False)):
                self._json(200, {"dryRun": True, "state": candidate})
                return

            started = time.time()
            try:
                output = _apply(candidate)
                state.update(candidate)
                last_applied_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                last_error = None
                self._json(200, {
                    "state": state,
                    "appliedAt": last_applied_at,
                    "durationMs": int((time.time() - started) * 1000),
                    "tc": output,
                })
            except Exception as exc:
                last_error = str(exc)
                self._json(500, {"error": "tc apply failed", "details": last_error})


if __name__ == "__main__":
    port = int(os.getenv("NETEM_HTTP_PORT", "18080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"netem-http: listening on :{port}", flush=True)
    server.serve_forever()
