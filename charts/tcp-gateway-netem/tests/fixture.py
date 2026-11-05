"""Synthetic TCP endpoints and probes for isolated container validation."""

import argparse
import json
import socket
import socketserver
import statistics
import subprocess
import time
import urllib.request


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        while data := self.request.recv(65536):
            self.request.sendall(data)


def probe(host, port, count):
    samples = []
    with socket.create_connection((host, port), timeout=5) as stream:
        stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for index in range(count + 20):
            payload = index.to_bytes(8, "big") * 4
            start = time.perf_counter_ns()
            stream.sendall(payload)
            result = b""
            while len(result) < len(payload):
                data = stream.recv(len(payload) - len(result))
                if not data:
                    raise RuntimeError("unexpected EOF")
                result += data
            assert result == payload
            if index >= 20:
                samples.append((time.perf_counter_ns() - start) / 1e6)
        stream.shutdown(socket.SHUT_WR)
        assert stream.recv(1) == b""
    samples.sort()
    return {
        "median_ms": statistics.median(samples),
        "p95_ms": samples[int(len(samples) * 0.95)],
        "samples": len(samples),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["server", "probe", "api"])
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--port", type=int, default=19002)
    parser.add_argument("--body")
    args = parser.parse_args()
    if args.mode == "server":
        for port in (19000, 19001):
            subprocess.Popen(
                ["iperf3", "-s", "-p", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        with socketserver.ThreadingTCPServer(("0.0.0.0", 19002), Echo) as server:
            server.serve_forever()
    elif args.mode == "probe":
        print(json.dumps(probe(args.host, args.port, args.count)))
    else:
        body = args.body.encode() if args.body is not None else None
        request = urllib.request.Request(
            args.host, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            print(response.read().decode())
