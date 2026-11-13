"""check Helm packaging, host mounts and namespace-scoped permissions."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]


class ChartTest(unittest.TestCase):
    def render(self, values=None):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "values.yaml"
            file.write_text(yaml.safe_dump(values or {}))
            text = subprocess.check_output(
                [
                    "helm",
                    "template",
                    "cpu-pool-control",
                    str(CHART),
                    "--namespace",
                    "experiment",
                    "-f",
                    str(file),
                ],
                text=True,
            )
        return {d["kind"]: d for d in yaml.safe_load_all(text) if d}

    def test_default_is_inactive_and_rbac_is_read_only(self):
        docs = self.render()
        config = json.loads(docs["ConfigMap"]["data"]["config.json"])
        self.assertIs(config["active"], False)
        self.assertEqual(config["pools"], {})
        self.assertEqual(config["checkInterval"], "5s")
        self.assertEqual(
            docs["Role"]["rules"],
            [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}],
        )
        self.assertEqual(docs["RoleBinding"]["subjects"][0]["namespace"], "experiment")
        pod = docs["DaemonSet"]["spec"]["template"]["spec"]
        self.assertNotIn("hostPID", pod)
        self.assertNotIn("hostNetwork", pod)
        self.assertTrue(pod["containers"][0]["securityContext"]["privileged"])
        self.assertEqual(
            {v["hostPath"]["path"] for v in pod["volumes"] if "hostPath" in v},
            {"/sys/fs/cgroup", "/sys/devices/system/cpu"},
        )

    def test_pools_and_frequency_caps_roundtrip_without_restarting_controller(self):
        base = self.render()
        docs = self.render(
            {
                "active": True,
                "nodeSelector": {"kubernetes.io/hostname": "onboard"},
                "pools": {
                    "ads": {"cpus": "0,2", "maxFrequencyMHz": 2000},
                    "simulation": {"cpus": "1,3"},
                },
            }
        )
        config = json.loads(docs["ConfigMap"]["data"]["config.json"])
        self.assertEqual(config["pools"]["ads"]["maxFrequencyMHz"], 2000)
        template = docs["DaemonSet"]["spec"]["template"]
        self.assertEqual(
            template["spec"]["nodeSelector"]["kubernetes.io/hostname"], "onboard"
        )
        self.assertEqual(
            template["metadata"]["annotations"],
            base["DaemonSet"]["spec"]["template"]["metadata"]["annotations"],
        )
        self.assertTrue(
            all(
                "subPath" not in m
                for m in template["spec"]["containers"][0]["volumeMounts"]
            )
        )
        self.assertEqual(
            docs["ConfigMap"]["data"]["controller.py"],
            (CHART / "files/controller.py").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
