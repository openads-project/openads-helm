"""Validate generic forwarding, shared shaping and proxy overhead on a private Docker network."""

import argparse
import json
import os
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]


def command(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def main(output):
    prefix = f"tcp-gateway-test-{os.getpid()}"
    containers = []
    images = yaml.safe_load((CHART / "values.yaml").read_text())
    report = {
        "proxy_image": images["image"],
        "controller_image": images["netem"]["image"],
    }
    network_created = False
    with tempfile.TemporaryDirectory(prefix="tcp-gateway-test-") as directory:
        directory = Path(directory)

        def start(name, args):
            full_name = f"{prefix}-{name}"
            command(["docker", "run", "-d", "--name", full_name, *args])
            containers.append(full_name)
            return full_name

        def run(name, args):
            return command(["docker", "exec", f"{prefix}-{name}", *args])

        def probe(target, count=500, client="client"):
            return json.loads(
                run(
                    client,
                    [
                        "python3",
                        "/tests/fixture.py",
                        "probe",
                        target,
                        "--count",
                        str(count),
                    ],
                )
            )

        def api(name, body=None):
            args = [
                "python3",
                "/tests/fixture.py",
                "api",
                f"http://{name}:18080/v1/netem/"
                + ("apply" if body is not None else "state"),
            ]
            if body is not None:
                args += ["--body", json.dumps(body)]
            return json.loads(run("client", args))

        def cpu():
            result = {}
            for name in ("gateway-a", "gateway-b"):
                stats = dict(
                    line.split()
                    for line in run(
                        name, ["cat", "/sys/fs/cgroup/cpu.stat"]
                    ).splitlines()
                )
                result[name] = int(stats["usage_usec"]) / 1e6
            return result

        def throughput(target, port=19000, rate=None, duration=3):
            args = [
                "iperf3",
                "-c",
                target,
                "-p",
                str(port),
                "-t",
                str(duration),
                "-O",
                "1",
                "-J",
            ]
            if rate:
                args += ["-b", rate]
            return json.loads(run("client", args))

        try:
            command(["docker", "network", "create", "--internal", prefix])
            network_created = True
            # render the actual chart configuration; start proxies before their destination exists
            for name, target in (("gateway-a", "gateway-b"), ("gateway-b", "server")):
                values = {
                    "forwards": [
                        {
                            "name": f"stream-{index}",
                            "listenPort": 19000 + index,
                            "targetHost": target,
                            "targetPort": 19000 + index,
                        }
                        for index in range(3)
                    ],
                    "netem": {"enabled": False},
                }
                file = directory / f"{name}.yaml"
                file.write_text(yaml.safe_dump(values))
                docs = [
                    doc
                    for doc in yaml.safe_load_all(
                        command(["helm", "template", name, str(CHART), "-f", str(file)])
                    )
                    if doc
                ]
                config = next(
                    doc["data"]["haproxy.cfg"]
                    for doc in docs
                    if doc["kind"] == "ConfigMap"
                )
                path = directory / f"{name}.cfg"
                path.write_text(config)
                start(
                    name,
                    [
                        "--network",
                        prefix,
                        "--network-alias",
                        name,
                        "-v",
                        f"{path}:/config/haproxy.cfg:ro",
                        images["image"],
                        "haproxy",
                        "-W",
                        "-db",
                        "-f",
                        "/config/haproxy.cfg",
                    ],
                )
            fixture = f"{CHART / 'tests'}:/tests:ro"
            start(
                "server",
                [
                    "--network",
                    prefix,
                    "--network-alias",
                    "server",
                    "-v",
                    fixture,
                    images["netem"]["image"],
                    "python3",
                    "/tests/fixture.py",
                    "server",
                ],
            )
            start(
                "client",
                [
                    "--network",
                    prefix,
                    "-v",
                    fixture,
                    images["netem"]["image"],
                    "sleep",
                    "infinity",
                ],
            )
            deadline = time.monotonic() + 20
            while True:
                try:
                    probe("gateway-a", count=5)
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            report["late_destination_and_half_close"] = True
            report["unshaped"] = {}
            for target in ("server", "gateway-b", "gateway-a"):
                latency = probe(target)
                samples = []
                for _ in range(3):
                    before = cpu()
                    start_time = time.monotonic()
                    measured = throughput(target)
                    elapsed = time.monotonic() - start_time
                    after = cpu()
                    samples.append(
                        {
                            "received_mbit_s": measured["end"]["sum_received"][
                                "bits_per_second"
                            ]
                            / 1e6,
                            "proxy_cpu_seconds": {
                                name: after[name] - before[name] for name in before
                            },
                            "wall_seconds": elapsed,
                        }
                    )
                before = cpu()
                start_time = time.monotonic()
                paced = throughput(target, rate="20M", duration=2)
                elapsed = time.monotonic() - start_time
                after = cpu()
                report["unshaped"][target] = {
                    "echo_rtt": latency,
                    "bulk_trials": samples,
                    "median_bulk_mbit_s": statistics.median(
                        s["received_mbit_s"] for s in samples
                    ),
                    "paced_20mbit": {
                        "received_mbit_s": paced["end"]["sum_received"][
                            "bits_per_second"
                        ]
                        / 1e6,
                        "proxy_cpu_percent_of_one_core": {
                            name: (after[name] - before[name]) / elapsed * 100
                            for name in before
                        },
                    },
                }
                print(f"unshaped benchmark completed: {target}", flush=True)

            # controller containers share only their respective disposable proxy network namespaces
            for name, peer in (("gateway-a", "gateway-b"), ("gateway-b", "gateway-a")):
                start(
                    "control-" + name[-1],
                    [
                        "--network",
                        f"container:{prefix}-{name}",
                        "--cap-add",
                        "NET_ADMIN",
                        "-v",
                        f"{CHART / 'files/netem'}:/netem:ro",
                        "-v",
                        fixture,
                        "-e",
                        "IFACE=eth0",
                        "-e",
                        f"PEER_HOST={peer}",
                        "-e",
                        "DELAY=20ms",
                        "-e",
                        "JITTER=0ms",
                        "-e",
                        "LOSS=0%",
                        "-e",
                        "RATE=8mbit",
                        "-e",
                        "DIST=normal",
                        "-e",
                        "LIMIT=1000",
                        "-e",
                        "NETEM_HTTP_PORT=18080",
                        images["netem"]["image"],
                        "sh",
                        "-ec",
                        "python3 /netem/apply.py && exec python3 /netem/server.py",
                    ],
                )
            deadline = time.monotonic() + 10
            while True:
                try:
                    api("gateway-a")
                    api("gateway-b")
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            streams = [
                subprocess.Popen(
                    [
                        "docker",
                        "exec",
                        f"{prefix}-client",
                        "iperf3",
                        "-c",
                        "gateway-a",
                        "-p",
                        str(port),
                        "-t",
                        "5",
                        "-O",
                        "1",
                        "-J",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for port in (19000, 19001)
            ]
            rates = []
            for process in streams:
                stdout, stderr = process.communicate(timeout=25)
                assert process.returncode == 0, stderr
                rates.append(
                    json.loads(stdout)["end"]["sum_received"]["bits_per_second"] / 1e6
                )
            assert 5.5 < sum(rates) < 9.5, rates
            shaped_latency = probe("gateway-a", count=20)
            local_latency = probe("server", count=50, client="control-b")
            assert shaped_latency["median_ms"] > 35, shaped_latency
            assert local_latency["median_ms"] < 5, local_latency
            report["shaped"] = {
                "class_limit_mbit_s": 8,
                "concurrent_stream_mbit_s": rates,
                "echo_rtt": shaped_latency,
                "local_bypass_rtt": local_latency,
            }
            for name in ("gateway-a", "gateway-b"):
                assert (
                    api(name, {"rate": "4mbit", "delay": "10ms"})["state"]["RATE"]
                    == "4mbit"
                )
            updated = (
                throughput("gateway-a", duration=5)["end"]["sum_received"][
                    "bits_per_second"
                ]
                / 1e6
            )
            assert 2.5 < updated < 4.8, updated
            report["updated_rate_mbit_s"] = updated
            report["limitations"] = [
                "same-host private Docker network; not a production WAN benchmark",
                "proxy cgroup CPU counters include the small cost of reading those counters",
                "bulk runs saturate one TCP connection; paced runs target 20 Mbit/s",
            ]
            output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)
        finally:
            for name in reversed(containers):
                subprocess.run(
                    ["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, check=False
                )
            if network_created:
                subprocess.run(
                    ["docker", "network", "rm", prefix],
                    stdout=subprocess.DEVNULL,
                    check=False,
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/tcp-gateway-validation.json")
    )
    main(parser.parse_args().output)
