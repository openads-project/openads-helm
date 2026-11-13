"""apply shared CPU masks and optional frequency caps to labelled node-local pods.

The Kubernetes API supplies pod identities; host cgroup and CPUFreq files enforce
the limits. The baseline is unrestricted: cleanup clears explicit pod CPU masks
and resets managed frequency policies to their hardware minimum and maximum.
Only managed paths are remembered in memory; no original settings are stored.
"""

import json
import logging
import os
import re
import signal
import ssl
import threading
import urllib.parse
import urllib.request
from pathlib import Path

LOG = logging.getLogger("cpu-pool-control")


def check_interval(value):
    """parse a check interval expressed in seconds, such as ``5s`` or ``1.5s``."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(value))
    if not match or not 1 <= float(match[1]) <= 60:
        raise ValueError("checkInterval must be between 1s and 60s")
    return float(match[1])


def cpus(text):
    """expand a Linux CPU list, for example ``0-3,8``, into CPU identifiers."""
    result = set()
    for part in str(text).replace(" ", ",").strip().split(","):
        if not part:
            continue
        if not re.fullmatch(r"\d+(?:-\d+)?", part):
            raise ValueError(f"invalid CPU list: {text!r}")
        ends = [int(n) for n in part.split("-")]
        first, last = ends[0], ends[-1]
        if first > last or last > 65535:
            raise ValueError(f"invalid CPU range: {part}")
        result.update(range(first, last + 1))
    if not result:
        raise ValueError("CPU list must not be empty")
    return result


def pod_cgroup(pod):
    """derive a systemd pod cgroup path from its Kubernetes UID and QoS class.

    Restricting this parent cgroup also restricts its containers and processes,
    including processes started later through kubectl exec.
    """
    uid = pod["metadata"]["uid"]
    if not re.fullmatch(r"[0-9a-f-]{36}", uid):
        raise ValueError(f"invalid pod UID: {uid}")
    qos = pod["status"].get("qosClass")
    parent = {
        "Guaranteed": "",
        "Burstable": "burstable",
        "BestEffort": "besteffort",
    }.get(qos)
    if parent is None:
        raise ValueError(f"waiting for pod QoS: {pod['metadata']['name']}")
    prefix = "kubepods" + (f"-{parent}" if parent else "")
    path = Path("kubepods.slice")
    if parent:
        path /= prefix + ".slice"
    return str(path / f"{prefix}-pod{uid.replace('-', '_')}.slice")


def write_limits(policy, minimum, maximum):
    """order writes so neither crosses the current opposite limit."""
    low, high = policy / "scaling_min_freq", policy / "scaling_max_freq"
    if minimum > int(high.read_text()):
        high.write_text(str(maximum))
        low.write_text(str(minimum))
    else:
        low.write_text(str(minimum))
        high.write_text(str(maximum))


class Controller:
    """reconcile one node's pools and reset managed paths when they leave a pool.

    ``managed_cgroups`` and ``managed_frequencies`` track cleanup work only for
    this process. On first reconciliation, remaining labels and pool definitions
    let a replacement controller rediscover restrictions from an earlier crash.
    """

    def __init__(self, cgroup=Path("/host-cgroup"), cpu=Path("/host-cpu")):
        """accept alternate filesystem roots so tests never write host settings."""
        self.cgroup, self.cpu = cgroup, cpu
        self.managed_cgroups = set()
        self.managed_frequencies = set()
        self.initialized = False

    def recover(self, config, pods):
        """rediscover labelled pod masks and capped policies after a restart.

        Policies outside the currently configured CPU pools and pods whose labels
        were removed cannot be rediscovered without persistent history.
        """
        for pod in pods:
            if pod["status"].get("phase") in {"Succeeded", "Failed"} or not pod[
                "status"
            ].get("qosClass"):
                continue
            path = pod_cgroup(pod)
            if (self.cgroup / path / "cpuset.cpus").exists():
                self.managed_cgroups.add(path)
        for pool in config["pools"].values():
            mask = cpus(pool["cpus"])
            for number in mask:
                policy = (self.cpu / f"cpu{number}/cpufreq").resolve()
                if not policy.is_dir():
                    continue
                if policy.parent != (self.cpu / "cpufreq").resolve():
                    raise ValueError(f"unsupported CPUFreq policy path: {policy}")
                related = cpus((policy / "related_cpus").read_text()) & cpus(
                    (self.cpu / "online").read_text()
                )
                if related <= mask and int(
                    (policy / "scaling_max_freq").read_text()
                ) < int((policy / "cpuinfo_max_freq").read_text()):
                    self.managed_frequencies.add(policy.name)
        self.initialized = True

    def plan(self, config, pods):
        """validate the complete desired policy before changing kernel settings.

        Pools must fit online/parent CPUs and must not share physical cores.
        Frequency caps additionally require complete CPUFreq policies and SMT
        siblings, because those hardware settings can affect multiple CPUs.
        """
        if not isinstance(config.get("active"), bool):
            raise TypeError("active must be a boolean")
        if not config["active"]:
            return {}, {}
        if "cpuset" not in (self.cgroup / "cgroup.controllers").read_text().split():
            raise ValueError("cgroup v2 with cpuset controller is required")
        online = cpus((self.cpu / "online").read_text())
        masks, owners, frequencies = {}, {}, {}
        pools = config["pools"]
        for name, pool in pools.items():
            mask = cpus(pool["cpus"])
            if not mask <= online:
                raise ValueError(f"pool {name} includes offline or absent CPUs")
            masks[name] = mask
            for number in mask:
                siblings = cpus(
                    (
                        self.cpu / f"cpu{number}/topology/thread_siblings_list"
                    ).read_text()
                )
                for sibling in siblings:
                    if sibling in owners and owners[sibling] != name:
                        raise ValueError(
                            f"pools {name} and {owners[sibling]} share a physical core"
                        )
                    owners[sibling] = name
            cap = pool.get("maxFrequencyMHz")
            if cap is None:
                continue
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise ValueError("maxFrequencyMHz must be a positive integer")
            for number in mask:
                siblings = (
                    cpus(
                        (
                            self.cpu / f"cpu{number}/topology/thread_siblings_list"
                        ).read_text()
                    )
                    & online
                )
                if not siblings <= mask:
                    raise ValueError(
                        "frequency caps require all online SMT siblings in the same pool"
                    )
                policy = (self.cpu / f"cpu{number}/cpufreq").resolve(strict=True)
                root = (self.cpu / "cpufreq").resolve()
                if policy.parent != root:
                    raise ValueError(f"unsupported CPUFreq policy path: {policy}")
                related = cpus((policy / "related_cpus").read_text()) & online
                if not related <= mask:
                    raise ValueError("CPUFreq policy crosses a pool boundary")
                maximum = cap * 1000
                if (
                    not int((policy / "cpuinfo_min_freq").read_text())
                    <= maximum
                    <= int((policy / "cpuinfo_max_freq").read_text())
                ):
                    raise ValueError(f"frequency cap outside hardware range: {cap} MHz")
                frequencies[policy.name] = maximum
        desired = {}
        for pod in pods:
            if pod["status"].get("phase") in {"Succeeded", "Failed"}:
                continue
            name = pod["metadata"].get("labels", {}).get(config["labelKey"])
            if name not in pools:
                raise ValueError(
                    f"unknown CPU pool {name!r} on {pod['metadata']['name']}"
                )
            path = pod_cgroup(pod)
            effective = self.cgroup / path / "cpuset.cpus.effective"
            if not effective.exists():
                raise ValueError(f"waiting for cgroup: {pod['metadata']['name']}")
            parent_mask = cpus(
                effective.parent.parent.joinpath("cpuset.cpus.effective").read_text()
            )
            if not masks[name] <= parent_mask:
                raise ValueError(f"pool {name} exceeds its parent cgroup's CPUs")
            desired[path] = ",".join(str(n) for n in sorted(masks[name]))
        return desired, frequencies

    def reset(self, cgroups=None, frequencies=None):
        """reset managed paths except those still present in the desired policy.

        Empty pod masks inherit the parent's CPUs. Frequency limits return to
        hardware bounds. Failed resets remain tracked for the next attempt;
        deleted pod cgroups are simply forgotten.
        """
        errors = []
        for path in list(self.managed_cgroups):
            if cgroups is not None and path in cgroups:
                continue
            try:
                target = self.cgroup / path / "cpuset.cpus"
                if target.exists():
                    target.write_text("\n")
                    if target.read_text().strip():
                        raise RuntimeError(f"CPU mask reset not effective: {path}")
                self.managed_cgroups.remove(path)
            except (OSError, RuntimeError) as error:
                errors.append(str(error))
        for name in list(self.managed_frequencies):
            if frequencies is not None and name in frequencies:
                continue
            try:
                policy = self.cpu / "cpufreq" / name
                minimum = int((policy / "cpuinfo_min_freq").read_text())
                maximum = int((policy / "cpuinfo_max_freq").read_text())
                write_limits(policy, minimum, maximum)
                if (
                    int((policy / "scaling_min_freq").read_text()) != minimum
                    or int((policy / "scaling_max_freq").read_text()) != maximum
                ):
                    raise RuntimeError(f"frequency reset not effective: {name}")
                self.managed_frequencies.remove(name)
            except (OSError, RuntimeError) as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("reset incomplete: " + "; ".join(errors))

    def reconcile(self, config, pods):
        """apply a validated policy, verify it and reset on a failed host write."""
        desired, frequencies = self.plan(config, pods)
        if not self.initialized:
            self.recover(config, pods)
        self.reset(desired, frequencies)
        try:
            for path, mask in desired.items():
                target = self.cgroup / path / "cpuset.cpus"
                self.managed_cgroups.add(path)
                current = target.read_text().strip()
                if not current or cpus(current) != cpus(mask):
                    target.write_text(mask)
                if cpus(target.with_name("cpuset.cpus.effective").read_text()) != cpus(
                    mask
                ):
                    raise RuntimeError(f"CPU mask not effective: {path}")
            for name, maximum in frequencies.items():
                policy = self.cpu / "cpufreq" / name
                self.managed_frequencies.add(name)
                minimum = int((policy / "cpuinfo_min_freq").read_text())
                if (
                    int((policy / "scaling_min_freq").read_text()),
                    int((policy / "scaling_max_freq").read_text()),
                ) != (minimum, maximum):
                    write_limits(policy, minimum, maximum)
                if int((policy / "scaling_max_freq").read_text()) > maximum:
                    raise RuntimeError(f"frequency cap not effective: {name}")
        except Exception:
            self.reset()
            raise
        return {
            "active": config["active"],
            "podCount": len(desired),
            "masks": desired,
            "frequencyCapsKHz": frequencies,
        }


def list_pods(node, namespace, label):
    """list labelled pods on this node using the mounted service-account token.

    TLS uses the cluster CA. The namespace-scoped list permission is sufficient;
    the controller neither patches workloads nor needs Kubernetes admin access.
    """
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    host = f"[{host}]" if ":" in host else host
    base = f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
    account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    query = urllib.parse.urlencode(
        {"fieldSelector": f"spec.nodeName={node}", "labelSelector": label}
    )
    request = urllib.request.Request(
        f"{base}/api/v1/namespaces/{namespace}/pods?{query}",
        headers={"Authorization": "Bearer " + (account / "token").read_text().strip()},
    )
    with urllib.request.urlopen(
        request,
        context=ssl.create_default_context(cafile=str(account / "ca.crt")),
        timeout=10,
    ) as response:
        return json.load(response)["items"]


def main():
    """reload the projected ConfigMap, reconcile and report readiness/status.

    API or validation errors leave the last policy in place and clear readiness.
    SIGTERM/SIGINT request a graceful reset; SIGKILL cannot perform cleanup.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    controller = Controller()
    ready, status = Path("/tmp/ready"), Path("/tmp/status.json")
    previous = None
    try:
        while not stop.is_set():
            interval = 5
            try:
                config = json.loads(Path("/config/config.json").read_text())
                interval = check_interval(config["checkInterval"])
                pods = list_pods(
                    os.environ["NODE_NAME"],
                    os.environ["POD_NAMESPACE"],
                    config["labelKey"],
                )
                current = controller.reconcile(config, pods)
                status.write_text(json.dumps(current, indent=2))
                ready.touch()
                if current != previous:
                    LOG.info("reconciled: %s", json.dumps(current))
                    previous = current
            except Exception as error:
                ready.unlink(missing_ok=True)
                status.write_text(json.dumps({"error": str(error)}))
                LOG.exception("reconciliation failed; not ready")
                interval = 5
            stop.wait(interval)
    finally:
        ready.unlink(missing_ok=True)
        controller.reset()
        LOG.info("managed settings reset to unrestricted defaults")


if __name__ == "__main__":
    main()
