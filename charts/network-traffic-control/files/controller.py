"""Apply shared receiver-side impairment to selected WAN flows."""

import argparse
import hashlib
import ipaddress
import json
import re
import signal
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


DEFAULT = {
    "delay": "0ms", "jitter": "0ms", "loss": "0%", "rate": "1000mbit",
    "distribution": "normal", "limit": 1000, "bypass": True,
}
QUANTITIES = {
    "delay": r"ms", "jitter": r"ms", "loss": r"%",
    "rate": r"(?:kbit|mbit|gbit)",
}


def run(*args):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=2,
    ).stdout


def validate(payload, previous):
    if not isinstance(payload, dict) or payload.keys() - (DEFAULT.keys() | {"dryRun"}):
        raise ValueError("expected only profile settings")
    profile = previous | {key: value for key, value in payload.items() if key != "dryRun"}
    for key, suffix in QUANTITIES.items():
        value = profile[key]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?" + suffix, value):
            raise ValueError(f"invalid {key}")
    if float(profile["loss"][:-1]) > 100 or not float(re.match(r"[0-9.]+", profile["rate"])[0]):
        raise ValueError("loss must be at most 100% and rate must be positive")
    if profile["distribution"] not in ("normal", "uniform", "pareto", "paretonormal"):
        raise ValueError("unsupported delay distribution")
    limit = str(profile["limit"])
    if not limit.isdecimal() or not 0 < int(limit) <= 2**32 - 1:
        raise ValueError("limit must be a positive packet count")
    profile["limit"] = int(limit)
    if type(profile["bypass"]) is not bool or type(payload.get("dryRun", False)) is not bool:
        raise ValueError("bypass and dryRun must be booleans")
    return profile


class TrafficControl:
    def __init__(self, config):
        self.ifb = config["ifb"]
        self.peer = str(ipaddress.IPv4Address(config["peer"]))
        self.priority = config["priority"]
        self.owner = "network-traffic-control:" + config["owner"]
        self.cookie = hashlib.sha256(self.owner.encode()).hexdigest()[:32]
        self.flows = []
        for flow in config["flows"]:
            if not isinstance(flow, dict) or len(flow) != 1:
                raise ValueError("each flow requires one TCP port selector")
            key, port = next(iter(flow.items()))
            selector = {"sourcePort": "src_port", "destinationPort": "dst_port"}.get(key)
            if not selector or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("invalid TCP port selector")
            self.flows.append((selector, str(port)))
        if not self.flows or len(set(self.flows)) != len(self.flows):
            raise ValueError("flows must be nonempty and unique")
        # reserve a small block so a reduced flow list can clean up older rules
        if type(self.priority) is not int or not 1 <= self.priority <= 65520 or len(self.flows) > 16:
            raise ValueError("invalid filter priority or more than 16 flows")
        self.default_profile = validate(config.get("netem", {}) | {"bypass": True}, DEFAULT)
        self.profile = self.default_profile.copy()
        self.error = None
        self.degraded = False

    def peer_route(self):
        routes = json.loads(run("ip", "-4", "-j", "route", "get", self.peer))
        if len(routes) != 1 or routes[0].get("type", "unicast") != "unicast":
            raise RuntimeError(f"expected one unicast IPv4 route to {self.peer}")
        interface = routes[0].get("dev")
        local = routes[0].get("prefsrc")
        if not interface or interface == self.ifb or not local or local == self.peer:
            raise RuntimeError(f"route to {self.peer} has no usable interface and source address")
        return interface, local

    def filters(self, interface):
        return json.loads(run("tc", "-j", "filter", "show", "dev", interface, "ingress"))

    def owned_priorities(self, interface, *, reject_conflicts=True):
        filters = {}
        for entry in self.filters(interface):
            priority = entry.get("pref", 0)
            if entry.get("chain", 0) == 0 and self.priority <= priority < self.priority + 16:
                filters.setdefault(priority, []).append(entry)
        owned = []
        for priority, entries in filters.items():
            rules = [entry for entry in entries if entry.get("options", {}).get("actions")]
            if (len(rules) != 1 or any(entry.get("kind") != "flower" or entry.get("protocol") != "ip"
                                     for entry in entries)
                    or not any(action.get("cookie") == self.cookie
                               for action in rules[0]["options"]["actions"])):
                if reject_conflicts:
                    raise RuntimeError(f"ingress priority {priority} belongs to another controller")
            else:
                owned.append(priority)
        return owned

    def remove_redirects(self, interface):
        for priority in self.owned_priorities(interface, reject_conflicts=False):
            run("tc", "filter", "del", "dev", interface, "ingress",
                "protocol", "ip", "pref", str(priority), "flower")

    def remove_stale_redirects(self):
        # a restarted controller or manual cleanup may see a different peer route
        for qdisc in json.loads(run("tc", "-j", "qdisc", "show")):
            if qdisc["kind"] == "clsact":
                self.remove_redirects(qdisc["dev"])

    def links(self):
        return {entry["ifname"]: entry for entry in json.loads(run("ip", "-j", "-d", "link", "show"))}

    def owns_ifb(self, link):
        if link.get("ifalias") != self.owner or link.get("linkinfo", {}).get("info_kind") != "ifb":
            raise RuntimeError(f"interface {self.ifb} belongs to another controller")

    def start(self):
        self.interface, self.local = self.peer_route()
        links = self.links()
        if self.interface not in links:
            raise RuntimeError(f"WAN interface {self.interface} does not exist")
        addresses = json.loads(run("ip", "-j", "-4", "addr", "show", "dev", self.interface))
        if not any(addr.get("local") == self.local for link in addresses for addr in link["addr_info"]):
            raise RuntimeError(f"{self.local} is not assigned to {self.interface}")
        print(f"WAN peer {self.peer} via {self.interface}, local IPv4 {self.local}")
        if self.ifb in links:
            self.owns_ifb(links[self.ifb])
        qdiscs = json.loads(run("tc", "-j", "qdisc", "show", "dev", self.interface))
        kinds = {entry["kind"] for entry in qdiscs}
        if "ingress" in kinds:
            raise RuntimeError("legacy ingress qdisc present; configure clsact before deployment")
        if "clsact" not in kinds:
            run("tc", "qdisc", "add", "dev", self.interface, "clsact")
        self.owned_priorities(self.interface)
        self.remove_stale_redirects()
        if self.ifb not in links:
            run("ip", "link", "add", self.ifb, "type", "ifb")
            try:
                run("ip", "link", "set", "dev", self.ifb, "alias", self.owner)
            except (OSError, subprocess.SubprocessError):
                run("ip", "link", "del", "dev", self.ifb)
                raise
        try:
            run("ip", "link", "set", "dev", self.ifb, "up")
            self.reset_queue()
        except (RuntimeError, OSError, subprocess.SubprocessError):
            self.close()
            raise

    def set_queue(self, profile, operation):
        run("tc", "class", operation, "dev", self.ifb, "parent", "1:", "classid", "1:11",
            "htb", "rate", profile["rate"])
        delay = profile["delay"]
        jitter = profile["jitter"]
        # send explicit zeros so changing a profile clears existing impairments
        command = ["tc", "qdisc", operation, "dev", self.ifb, "parent", "1:11",
                   "handle", "11:", "netem", "limit", str(profile["limit"]),
                   "delay", delay, jitter]
        if float(jitter[:-2]):
            command += ["distribution", profile["distribution"]]
        command += ["loss", profile["loss"]]
        run(*command)

    def install_redirects(self):
        if self.owned_priorities(self.interface):
            raise RuntimeError("redirects are already present")
        for index, (selector, port) in enumerate(self.flows):
            run("tc", "filter", "add", "dev", self.interface, "ingress", "protocol", "ip",
                "pref", str(self.priority + index), "flower", "skip_hw", "ip_proto", "tcp",
                "src_ip", self.peer, "dst_ip", self.local, selector, port,
                "action", "mirred", "egress", "redirect", "dev", self.ifb, "cookie", self.cookie)

    def reset_queue(self):
        qdiscs = json.loads(run("tc", "-j", "qdisc", "show", "dev", self.ifb))
        if any(entry.get("handle") == "1:" and entry.get("root") for entry in qdiscs):
            run("tc", "qdisc", "del", "dev", self.ifb, "root")
        run("tc", "qdisc", "add", "dev", self.ifb, "root", "handle", "1:", "htb", "default", "11")
        self.set_queue(self.default_profile, "replace")

    def bypass(self):
        self.remove_redirects(self.interface)
        # discard delayed packets so bypass cannot release a backlog into the baseline
        self.reset_queue()

    def apply(self, profile):
        previous = self.profile
        if profile == previous and self.error is None:
            return
        try:
            if profile["bypass"]:
                self.bypass()
            else:
                if previous["bypass"]:
                    self.bypass()
                self.set_queue(profile, "change")
                if previous["bypass"]:
                    self.install_redirects()
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self.error = explain(exc)
            try:
                if previous["bypass"]:
                    self.bypass()
                elif profile["bypass"]:
                    self.bypass()
                    self.set_queue(previous, "change")
                    self.install_redirects()
                else:
                    self.set_queue(previous, "change")
            except (RuntimeError, OSError, subprocess.SubprocessError) as rollback:
                self.degraded = True
                self.error += f"; restore failed: {explain(rollback)}"
                try:
                    self.remove_redirects(self.interface)
                    self.profile = self.default_profile.copy()
                except (RuntimeError, OSError, subprocess.SubprocessError) as bypass_error:
                    self.error += f"; bypass failed: {explain(bypass_error)}"
            raise RuntimeError(self.error) from exc
        self.profile = profile
        self.error = None
        self.degraded = False

    def close(self):
        self.remove_stale_redirects()
        link = self.links().get(self.ifb)
        if link:
            self.owns_ifb(link)
            run("ip", "link", "del", "dev", self.ifb)


def explain(error):
    return (getattr(error, "stderr", "") or str(error)).strip()


class Handler(BaseHTTPRequestHandler):
    timeout = 2

    def reply(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        controller = self.server.controller
        if self.path == "/health":
            self.reply(503 if controller.degraded else 200, {"ok": not controller.degraded})
        elif self.path == "/v1/netem/state":
            self.reply(200, {"state": controller.profile, "lastError": controller.error,
                             "degraded": controller.degraded, "ifb": controller.ifb})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/netem/apply":
            self.reply(404, {"error": "not found"})
            return
        controller = self.server.controller
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 4096:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(size))
            profile = validate(payload, controller.profile)
        except (ValueError, UnicodeError) as exc:
            self.reply(400, {"error": str(exc)})
            return
        if payload.get("dryRun"):
            self.reply(200, {"dryRun": True, "state": profile})
            return
        try:
            controller.apply(profile)
        except RuntimeError as exc:
            self.reply(500, {"error": str(exc)})
            return
        self.reply(200, {"state": profile})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    controller = TrafficControl(config)
    if args.cleanup:
        controller.close()
        return
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    with HTTPServer(("", config["httpPort"]), Handler) as server:
        server.timeout = 0.5
        server.controller = controller
        controller.start()
        try:
            while not stopping:
                server.handle_request()
        finally:
            controller.close()


if __name__ == "__main__":
    main()
