"""Exercise real tc and direct TCP connections in disposable network namespaces."""

from contextlib import contextmanager
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
import uuid


CONTROLLER = Path(__file__).resolve().parents[1] / "files"
SERVER = """
import socket, sys
with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", int(sys.argv[1])))
    listener.listen()
    print("ready", flush=True)
    while True:
        conn, peer = listener.accept()
        with conn:
            mode = conn.recv(1)
            if mode == b'b':
                while conn.recv(65536):
                    pass
            if mode:
                conn.sendall(peer[0].encode())
"""
PROBE = """
import json, socket, sys, time
start = time.monotonic()
with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1) as conn:
    if len(sys.argv) > 3:
        conn.settimeout(12)
        conn.sendall(b'b' + bytes(512 * 1024))
        conn.shutdown(socket.SHUT_WR)
    else:
        conn.sendall(b'x')
    peer = conn.recv(128).decode()
print(json.dumps([time.monotonic() - start, peer]))
"""


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=10).stdout


@unittest.skipUnless(os.getenv("NTC_KERNEL_TEST") == "1", "requires isolated kernel test privileges")
class TestKernel(unittest.TestCase):
    def test_direct_connections_and_ownership(self):
        suffix = uuid.uuid4().hex[:6]
        left, right = "ntc-l-" + suffix, "ntc-r-" + suffix
        for namespace in (left, right):
            run("ip", "netns", "add", namespace)
            self.addCleanup(run, "ip", "netns", "del", namespace)
        run("ip", "link", "add", "wan0", "netns", left, "type", "veth",
            "peer", "name", "wan0", "netns", right)
        for namespace, address in ((left, "192.0.2.1"), (right, "192.0.2.2")):
            self.ns(namespace, "ip", "addr", "add", address + "/24", "dev", "wan0")
            self.ns(namespace, "ip", "link", "set", "wan0", "up")
            self.ns(namespace, "ip", "link", "set", "lo", "up")
        self.ns(right, "tc", "qdisc", "add", "dev", "wan0", "clsact")
        self.ns(right, "tc", "filter", "add", "dev", "wan0", "ingress",
                "protocol", "ip", "pref", "10", "flower", "ip_proto", "udp", "action", "pass")
        for port in (7448, 8554, 7447):
            server = subprocess.Popen(
                ["ip", "netns", "exec", right, sys.executable, "-c", SERVER, str(port)],
                stdout=subprocess.PIPE, text=True,
            )
            self.addCleanup(server.stdout.close)
            self.addCleanup(server.wait, 3)
            self.addCleanup(server.terminate)
            self.assertEqual(server.stdout.readline().strip(), "ready")
        with self.control(right) as controller:
            for port in (7448, 8554):
                delay, peer = self.probe(left, port)
                self.assertEqual(peer, "192.0.2.1")
                self.assertLess(delay, 0.08)
            self.command(right, '{"bypass":false}')
            for port in (7448, 8554):
                delay, peer = self.probe(left, port)
                self.assertEqual(peer, "192.0.2.1")
                self.assertGreaterEqual(delay, 0.16)
            delay, _ = self.probe(left, 7447)
            self.assertLess(delay, 0.08)
            self.command(right, '{"loss":"100%","bypass":false}')
            with self.assertRaises(subprocess.CalledProcessError):
                self.probe(left, 7448)
            self.command(right, '{"loss":"0%","delay":"35ms","bypass":false}')
            stats = self.ns(right, "tc", "-s", "class", "show", "dev", "ifb-test", "classid", "1:11")
            sent = re.search(r"\bSent (\d+) bytes\b", stats)
            self.assertIsNotNone(sent, stats)
            self.assertGreater(int(sent[1]), 0)
            delay, _ = self.probe(left, 7448)
            self.assertGreaterEqual(delay, 0.07)
            self.command(right, '{"rate":"2mbit","delay":"0ms","bypass":false}')
            self.check_shared_cap(left)
            self.command(right, '{"bypass":true}')
            delay, _ = self.probe(left, 7448)
            self.assertLess(delay, 0.07)
            with self.control(left, "192.0.2.1", "192.0.2.2", [{"sourcePort": 7448}]):
                self.command(left, '{"delay":"60ms","bypass":false}', "192.0.2.1")
                delay, peer = self.probe(left, 7448)
                self.assertGreaterEqual(delay, 0.12)
                self.assertEqual(peer, "192.0.2.1")
                delay, _ = self.probe(left, 8554)
                self.assertLess(delay, 0.08)
            self.command(right, '{"delay":"80ms","bypass":false}')
            controller.kill()
            controller.wait(timeout=5)
        # abrupt termination leaves kernel state, which startup must recognize and clear
        stale = json.loads(self.ns(right, "tc", "-j", "filter", "show", "dev", "wan0", "ingress"))
        self.assertIn(49000, {entry["pref"] for entry in stale})
        with self.control(right):
            delay, _ = self.probe(left, 7448)
            self.assertLess(delay, 0.08)
            self.command(right, '{"delay":"40ms","bypass":false}')
        filters = json.loads(self.ns(right, "tc", "-j", "filter", "show", "dev", "wan0", "ingress"))
        self.assertEqual({entry["pref"] for entry in filters}, {10})
        self.assertNotIn("ifb-test", self.ns(right, "ip", "-j", "link", "show"))

    @staticmethod
    def ns(namespace, *args):
        return run("ip", "netns", "exec", namespace, *args)

    def probe(self, namespace, port):
        return json.loads(self.ns(namespace, sys.executable, "-c", PROBE, "192.0.2.2", str(port)))

    def check_shared_cap(self, namespace):
        command = ["ip", "netns", "exec", namespace, sys.executable, "-c", PROBE, "192.0.2.2"]
        with subprocess.Popen(command + ["7448", "bulk"], stdout=subprocess.PIPE, text=True) as first:
            with subprocess.Popen(command + ["8554", "bulk"], stdout=subprocess.PIPE, text=True) as second:
                times = []
                for client in (first, second):
                    stdout, _ = client.communicate(timeout=14)
                    self.assertEqual(client.returncode, 0)
                    elapsed, _ = json.loads(stdout)
                    times.append(elapsed)
        # the aggregate transfer is about 4.2 s at 2 Mbit/s, versus 2.1 s with separate caps
        self.assertGreater(max(times), 3.5)
        self.assertLess(max(times), 8)

    @staticmethod
    def command(namespace, payload, server="192.0.2.2"):
        code = """
import sys, urllib.request
request = urllib.request.Request(f'http://{sys.argv[2]}:18080/v1/netem/apply', data=sys.argv[1].encode())
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(request, timeout=4) as response:
    print(response.read().decode())
"""
        return run("ip", "netns", "exec", namespace, sys.executable, "-c", code, payload, server)

    @staticmethod
    @contextmanager
    def control(namespace, local="192.0.2.2", peer="192.0.2.1", flows=None):
        with TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({
                "ifb": "ifb-test", "priority": 49000,
                "peer": peer, "owner": "test/controller",
                "flows": flows or [{"destinationPort": 7448}, {"destinationPort": 8554}],
                "netem": {"delay": "80ms"},
                "httpPort": 18080,
            }))
            process = subprocess.Popen([
                "ip", "netns", "exec", namespace, sys.executable,
                str(CONTROLLER / "controller.py"), str(config),
            ])
            try:
                for _ in range(50):
                    if process.poll() is not None:
                        raise RuntimeError(f"controller exited with {process.returncode}")
                    try:
                        run("ip", "netns", "exec", namespace, sys.executable, "-c",
                            "import sys, urllib.request as u; u.build_opener(u.ProxyHandler({})).open(sys.argv[1], timeout=0.2)",
                            f"http://{local}:18080/health")
                        break
                    except subprocess.CalledProcessError:
                        time.sleep(0.05)
                else:
                    raise RuntimeError("controller never became ready")
                yield process
            finally:
                if process.poll() is None:
                    process.terminate()
                    if process.wait(timeout=10):
                        raise RuntimeError(f"controller exited with {process.returncode}")
