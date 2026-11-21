"""Verify profile validation and failure handling without modifying the kernel."""

import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "netem_server", Path(__file__).resolve().parents[1] / "files/netem/server.py"
)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ControllerTest(unittest.TestCase):
    def request(self, body):
        request = object.__new__(server.Handler)
        request.path = "/v1/netem/apply"
        data = json.dumps(body).encode()
        request.headers = {"Content-Length": str(len(data))}
        request.rfile = io.BytesIO(data)
        request._json = Mock()
        return request

    def test_non_object_request_is_rejected(self):
        request = self.request([])
        request.do_POST()
        self.assertEqual(request._json.call_args.args[0], 400)

    def test_failed_profile_update_preserves_previous_state(self):
        state = {
            "IFACE": "eth0",
            "PEER_HOST": "192.0.2.2",
            "DELAY": "0ms",
            "JITTER": "0ms",
            "LOSS": "0%",
            "RATE": "8mbit",
            "DIST": "normal",
            "LIMIT": "1000",
        }
        with (
            patch.dict(server.state, state, clear=True),
            patch.object(
                server, "_apply", side_effect=RuntimeError("injected failure")
            ),
        ):
            request = self.request({"rate": "4mbit"})
            request.do_POST()
            self.assertEqual(request._json.call_args.args[0], 500)
            self.assertEqual(server.state["RATE"], "8mbit")

    def test_invalid_rate_and_dry_run_never_apply(self):
        state = {
            "IFACE": "eth0",
            "PEER_HOST": "192.0.2.2",
            "DELAY": "0ms",
            "JITTER": "0ms",
            "LOSS": "0%",
            "RATE": "8mbit",
            "DIST": "normal",
            "LIMIT": "1000",
        }
        with (
            patch.dict(server.state, state, clear=True),
            patch.object(server, "_apply") as apply,
        ):
            invalid = self.request({"rate": "fast"})
            invalid.do_POST()
            self.assertEqual(invalid._json.call_args.args[0], 400)
            dry_run = self.request({"rate": "4mbit", "dryRun": True})
            dry_run.do_POST()
            self.assertEqual(dry_run._json.call_args.args[0], 200)
            self.assertEqual(server.state["RATE"], "8mbit")
            apply.assert_not_called()
