import ipaddress
import json
import os
import re
import socket
import subprocess
import sys


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd)} failed: {(proc.stderr or proc.stdout).strip()}"
        )
    return proc


def resolve_ipv4s(peer_host: str) -> list[str]:
    if not peer_host:
        return []
    try:
        ipaddress.IPv4Address(peer_host)
        return [peer_host]
    except ipaddress.AddressValueError:
        pass

    resolved = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(
        peer_host, None, socket.AF_INET
    ):
        if family == socket.AF_INET:
            ip = sockaddr[0]
            if ip not in resolved:
                resolved.append(ip)
    return resolved


def ensure_htb(iface: str, rate: str) -> None:
    qdisc = run(["tc", "qdisc", "show", "dev", iface], check=False).stdout
    if "qdisc htb 1:" not in qdisc:
        run(["tc", "qdisc", "del", "dev", iface, "root"], check=False)
        run(
            [
                "tc",
                "qdisc",
                "add",
                "dev",
                iface,
                "root",
                "handle",
                "1:",
                "htb",
                "default",
                "0",
            ]
        )

    classes = run(["tc", "class", "show", "dev", iface], check=False).stdout
    if "class htb 1:11" not in classes:
        run(
            [
                "tc",
                "class",
                "add",
                "dev",
                iface,
                "parent",
                "1:",
                "classid",
                "1:11",
                "htb",
                "rate",
                rate,
            ]
        )
    else:
        run(
            [
                "tc",
                "class",
                "replace",
                "dev",
                iface,
                "parent",
                "1:",
                "classid",
                "1:11",
                "htb",
                "rate",
                rate,
            ]
        )


def is_zero_quantity(value: str, unit: str) -> bool:
    return re.fullmatch(rf"0+(?:\.0+)?{re.escape(unit)}", value) is not None


def apply_netem(
    iface: str, delay: str, jitter: str, dist: str, loss: str, limit: str
) -> None:
    cmd = [
        "tc",
        "qdisc",
        "replace",
        "dev",
        iface,
        "parent",
        "1:11",
        "handle",
        "11:",
        "netem",
    ]

    if not (is_zero_quantity(delay, "ms") and is_zero_quantity(jitter, "ms")):
        cmd.extend(["delay", delay])
        if not is_zero_quantity(jitter, "ms"):
            cmd.append(jitter)
            if dist:
                cmd.extend(["distribution", dist])

    if not is_zero_quantity(loss, "%"):
        cmd.extend(["loss", loss])

    if limit:
        cmd.extend(["limit", limit])
    run(cmd)


def update_filters(iface: str, target_ips: list[str]) -> None:
    filters = json.loads(
        run(
            [
                "tc",
                "-j",
                "filter",
                "show",
                "dev",
                iface,
                "protocol",
                "ip",
                "parent",
                "1:",
            ]
        ).stdout
    )
    for entry in filters:
        if (
            entry["kind"] != "u32"
            or entry.get("chain", 0) != 0
            or entry.get("options", {}).get("flowid", "1:11") != "1:11"
        ):
            raise RuntimeError("unexpected filter in controller-owned HTB root 1:")
    priorities = {int(entry["pref"]) for entry in filters}
    priority = next(
        (value for value in range(1, 65536) if value not in priorities), None
    )
    if priority is None:
        raise RuntimeError("no free filter priority for profile update")

    # keep all old matches until the complete replacement is installed, including after a failed apply
    for ip in target_ips:
        cmd = [
            "tc",
            "filter",
            "add",
            "dev",
            iface,
            "protocol",
            "ip",
            "parent",
            "1:",
            "prio",
            str(priority),
            "u32",
        ]
        cmd.extend(["match", "ip", "dst", f"{ip}/32"])
        cmd.extend(["flowid", "1:11"])
        run(cmd)

    # a retry also retires partial replacements left by an interrupted or failed update
    for old_priority in sorted(priorities):
        run(
            [
                "tc",
                "filter",
                "del",
                "dev",
                iface,
                "protocol",
                "ip",
                "parent",
                "1:",
                "prio",
                str(old_priority),
            ]
        )


def main() -> int:
    iface = env("IFACE", "eth0")
    peer_host = env("PEER_HOST", "")
    delay = env("DELAY", "0ms")
    jitter = env("JITTER", "0ms")
    loss = env("LOSS", "0%")
    rate = env("RATE", "1000mbit")
    dist = env("DIST", "normal")
    limit = env("LIMIT", "")

    target_ips = resolve_ipv4s(peer_host)
    if not target_ips:
        print(
            "netem: peerHost must resolve to at least one IPv4 address", file=sys.stderr
        )
        return 1

    try:
        ensure_htb(iface, rate)
        apply_netem(iface, delay, jitter, dist, loss, limit)
        update_filters(iface, target_ips)
    except RuntimeError as exc:
        print(f"netem: {exc}", file=sys.stderr)
        return 1

    print(f"netem: peerHost={peer_host} targetIps={','.join(target_ips)}")

    show = run(["tc", "-s", "qdisc", "show", "dev", iface], check=False)
    if show.stdout:
        print(show.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
