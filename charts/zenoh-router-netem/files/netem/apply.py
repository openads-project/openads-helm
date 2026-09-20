#!/usr/bin/env python3
import ipaddress
import os
import re
import socket
import struct
import subprocess
import sys


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def is_true(value: str) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def parse_remote_ip(hex_addr: str) -> str:
    return socket.inet_ntoa(struct.pack("<L", int(hex_addr, 16)))


def resolve_ipv4s(peer_host: str) -> list[str]:
    if not peer_host:
        return []
    try:
        ipaddress.IPv4Address(peer_host)
        return [peer_host]
    except ipaddress.AddressValueError:
        pass

    resolved = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(peer_host, None, socket.AF_INET):
        if family == socket.AF_INET:
            ip = sockaddr[0]
            if ip not in resolved:
                resolved.append(ip)
    return resolved


def detect_live_peer_ip(candidates: list[str], zenoh_port: int) -> str:
    candidate_set = set(candidates)
    try:
        with open("/proc/net/tcp", "r", encoding="utf-8") as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) < 4 or parts[3] != "01":
                    continue
                try:
                    local_hex, remote_hex = parts[1], parts[2]
                    _, local_port_hex = local_hex.split(":")
                    remote_addr_hex, remote_port_hex = remote_hex.split(":")
                    remote_ip = parse_remote_ip(remote_addr_hex)
                    local_port = int(local_port_hex, 16)
                    remote_port = int(remote_port_hex, 16)
                except Exception:
                    continue

                if candidate_set and remote_ip not in candidate_set:
                    continue
                if local_port == zenoh_port or remote_port == zenoh_port:
                    return remote_ip
    except FileNotFoundError:
        return ""
    return ""


def ensure_htb(iface: str, rate: str) -> None:
    qdisc = run(["tc", "qdisc", "show", "dev", iface], check=False).stdout
    if "qdisc htb 1:" not in qdisc:
        run(["tc", "qdisc", "del", "dev", iface, "root"], check=False)
        run(["tc", "qdisc", "add", "dev", iface, "root", "handle", "1:", "htb", "default", "10"])

    classes = run(["tc", "class", "show", "dev", iface], check=False).stdout
    if "class htb 1:10" not in classes:
        run(["tc", "class", "add", "dev", iface, "parent", "1:", "classid", "1:10", "htb", "rate", "1000mbit"])
    if "class htb 1:11" not in classes:
        run(["tc", "class", "add", "dev", iface, "parent", "1:", "classid", "1:11", "htb", "rate", rate])
    else:
        run(["tc", "class", "replace", "dev", iface, "parent", "1:", "classid", "1:11", "htb", "rate", rate])


def apply_netem(iface: str, delay: str, jitter: str, dist: str, loss: str, limit: str) -> None:
    cmd = [
        "tc", "qdisc", "replace", "dev", iface, "parent", "1:11", "handle", "11:", "netem",
        "delay", delay, jitter, "distribution", dist, "loss", loss,
    ]
    if limit:
        cmd.extend(["limit", limit])
    run(cmd)


def update_filters(iface: str, target_ips: list[str], peer_port: str) -> None:
    run(["tc", "filter", "del", "dev", iface, "protocol", "ip", "parent", "1:", "prio", "1"], check=False)

    if target_ips and peer_port:
        for ip in target_ips:
            run([
                "tc", "filter", "add", "dev", iface, "protocol", "ip", "parent", "1:", "prio", "1", "u32",
                "match", "ip", "protocol", "6", "0xff",
                "match", "ip", "dst", f"{ip}/32",
                "match", "ip", "dport", peer_port, "0xffff",
                "flowid", "1:11",
            ])
        return

    if target_ips:
        for ip in target_ips:
            run([
                "tc", "filter", "add", "dev", iface, "protocol", "ip", "parent", "1:", "prio", "1", "u32",
                "match", "ip", "dst", f"{ip}/32",
                "flowid", "1:11",
            ])
        return

    run([
        "tc", "filter", "add", "dev", iface, "protocol", "ip", "parent", "1:", "prio", "1", "u32",
        "match", "ip", "protocol", "6", "0xff",
        "match", "ip", "dport", peer_port, "0xffff",
        "flowid", "1:11",
    ])


def main() -> int:
    iface = env("IFACE", "eth0")
    auto_peer = is_true(env("AUTO_PEER", "true"))
    peer_host = env("PEER_HOST", "")
    peer_port = env("PEER_PORT", "")
    delay = env("DELAY", "20ms")
    jitter = env("JITTER", "5ms")
    loss = env("LOSS", "0.5%")
    rate = env("RATE", "50mbit")
    dist = env("DIST", "normal")
    limit = env("LIMIT", "")

    zenoh_port_raw = env("ZENOH_PORT", "7447")
    if not re.match(r"^[0-9]+$", zenoh_port_raw):
        print(f"netem: invalid ZENOH_PORT={zenoh_port_raw}", file=sys.stderr)
        return 1
    zenoh_port = int(zenoh_port_raw)

    resolved_ips = resolve_ipv4s(peer_host)
    if peer_host and not resolved_ips:
        print(f"netem: failed to resolve peerHost={peer_host}", file=sys.stderr)
        return 1

    target_ips: list[str] = []
    if auto_peer and resolved_ips:
        live = detect_live_peer_ip(resolved_ips, zenoh_port)
        if live:
            target_ips = [live]
    if not target_ips:
        target_ips = resolved_ips

    if not target_ips and not peer_port:
        print("netem: set peerHost and/or peerPort", file=sys.stderr)
        return 1

    try:
        ensure_htb(iface, rate)
        apply_netem(iface, delay, jitter, dist, loss, limit)
        update_filters(iface, target_ips, peer_port)
    except RuntimeError as exc:
        print(f"netem: {exc}", file=sys.stderr)
        return 1

    print(
        "netem: "
        f"autoPeer={str(auto_peer).lower()} "
        f"peerHost={peer_host or '<none>'} "
        f"targetIps={','.join(target_ips) if target_ips else '<none>'} "
        f"peerPort={peer_port or '<all>'}"
    )

    show = run(["tc", "-s", "qdisc", "show", "dev", iface], check=False)
    if show.stdout:
        print(show.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
