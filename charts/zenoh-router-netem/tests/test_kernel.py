"""exercise traffic control in a disposable user/network namespace."""
import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "netem_apply", Path(__file__).resolve().parents[1] / "files/netem/apply.py"
)
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)


@unittest.skipUnless(os.getenv("NETEM_TEST_PARENT_NETNS"), "run this file directly for namespace isolation")
class KernelUpdateTest(unittest.TestCase):
    def test_repeated_updates_keep_packets_shaped_and_local_traffic_unshaped(self):
        # refuse to modify the host namespace even if this file is invoked directly
        self.assertNotEqual(Path('/proc/self/ns/net').readlink(), Path(os.environ["NETEM_TEST_PARENT_NETNS"]))
        subprocess.run(["ip", "link", "add", "netem-test", "type", "veth", "peer", "name", "netem-sink"], check=True)
        try:
            for iface in ("netem-test", "netem-sink"):
                subprocess.run(["ip", "link", "set", iface, "up"], check=True)
            subprocess.run(["ip", "addr", "add", "192.0.2.1/24", "dev", "netem-test"], check=True)
            mac = json.loads(subprocess.check_output(["ip", "-j", "link", "show", "netem-sink"]))[0]["address"]
            for target in ("192.0.2.2", "192.0.2.3"):
                subprocess.run(["ip", "neigh", "add", target, "lladdr", mac, "dev", "netem-test"], check=True)
            apply.ensure_htb("netem-test", "6mbit")
            apply.apply_netem("netem-test", "60ms", "0ms", "normal", "0%", "1000")
            apply.update_filters("netem-test", ["192.0.2.2"], "")
            received = {"192.0.2.2": [], "192.0.2.3": []}
            stop = threading.Event()
            with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800)) as rx, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
                rx.bind(("netem-sink", 0))
                rx.settimeout(0.1)

                def receive():
                    while not stop.is_set():
                        try:
                            frame = rx.recv(2048)
                        except TimeoutError:
                            continue
                        if frame[23] != 17:
                            continue
                        target = socket.inet_ntoa(frame[30:34])
                        ihl = (frame[14] & 15) * 4
                        payload = frame[14 + ihl + 8:]
                        if target in received and payload.startswith(b"NETEMTEST"):
                            received[target].append(time.monotonic() - struct.unpack("!d", payload[9:17])[0])

                def send():
                    while not stop.is_set():
                        for target in received:
                            tx.sendto(b"NETEMTEST" + struct.pack("!d", time.monotonic()), (target, 45000))
                        time.sleep(0.002)

                receiver = threading.Thread(target=receive)
                sender = threading.Thread(target=send)
                receiver.start()
                sender.start()
                try:
                    for index in range(80):
                        apply.ensure_htb("netem-test", "5mbit" if index % 2 else "6mbit")
                        apply.apply_netem("netem-test", "60ms" if index % 2 else "80ms", "0ms", "normal", "0%", "1000")
                        if index == 40:
                            with self.assertRaises(RuntimeError):
                                apply.update_filters("netem-test", ["192.0.2.2", "invalid-ip"], "")
                        apply.update_filters("netem-test", ["192.0.2.2"], "")
                    # allow delayed packets to drain before stopping the receiver
                    time.sleep(0.15)
                finally:
                    stop.set()
                    sender.join()
                    receiver.join()
            self.assertGreater(len(received["192.0.2.2"]), 100)
            self.assertGreater(len(received["192.0.2.3"]), 100)
            self.assertGreater(min(received["192.0.2.2"]), 0.05)
            self.assertLess(min(received["192.0.2.3"]), 0.02)
            print(json.dumps({target: {"packets": len(delays), "minDelayMs": round(min(delays)*1000, 3)} for target, delays in received.items()}))
        finally:
            subprocess.run(["ip", "link", "del", "netem-test"], check=True)


if __name__ == "__main__":
    if not os.getenv("NETEM_TEST_PARENT_NETNS"):
        test_env = dict(os.environ, NETEM_TEST_PARENT_NETNS=str(Path("/proc/self/ns/net").readlink()))
        sys.exit(subprocess.run([
            "unshare", "--user", "--map-root-user", "--net", sys.executable, __file__,
        ], env=test_env, check=False).returncode)
    unittest.main()
