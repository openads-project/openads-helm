"""Verify generic listener configuration, isolation and shared shaping resources."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]


class ChartTest(unittest.TestCase):
    def render(self, forwards=None, netem=None, success=True):
        values = {
            "forwards": forwards
            if forwards is not None
            else [
                {
                    "name": "stream-a",
                    "listenPort": 19000,
                    "targetHost": "target-a",
                    "targetPort": 20000,
                },
                {
                    "name": "stream-b",
                    "listenPort": 19001,
                    "targetHost": "target-b",
                    "targetPort": 20001,
                },
            ],
            "netem": {"peerHost": "192.0.2.2", **(netem or {})},
        }
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "values.yaml"
            file.write_text(yaml.safe_dump(values))
            result = subprocess.run(
                ["helm", "template", "gateway", str(CHART), "-f", str(file)],
                text=True,
                capture_output=True,
                check=False,
            )
        if not success:
            self.assertNotEqual(result.returncode, 0)
            return result.stderr
        self.assertEqual(result.returncode, 0, result.stderr)
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def test_empty_forward_list_creates_no_resources(self):
        self.assertEqual(self.render(forwards=[]), [])

    def test_arbitrary_listener_count_shares_one_controller(self):
        forwards = [
            {
                "name": f"stream-{index}",
                "listenPort": 19000 + index,
                "targetHost": f"target-{index}",
                "targetPort": 20000 + index,
            }
            for index in range(7)
        ]
        docs = self.render(forwards=forwards)
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(
            [c["name"] for c in pod["containers"]], ["proxy", "netem-http"]
        )
        self.assertEqual(len(pod["containers"][0]["ports"]), 7)
        self.assertFalse(pod.get("hostNetwork", False))
        self.assertNotIn("strategy", deployment["spec"])
        cfg = next(d["data"]["haproxy.cfg"] for d in docs if d["kind"] == "ConfigMap")
        self.assertIn("option splice-auto", cfg)
        self.assertEqual(cfg.count("listen stream-"), 7)
        self.assertEqual(
            pod["containers"][1]["securityContext"]["capabilities"]["add"],
            ["NET_ADMIN"],
        )

    def test_host_port_and_controller_exposure(self):
        docs = self.render(
            forwards=[
                {
                    "name": "stream-a",
                    "listenPort": 19000,
                    "hostPort": 20000,
                    "targetHost": "target",
                    "targetPort": 19000,
                }
            ],
            netem={"http": {"nodePort": 30880}},
        )
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        external = next(
            d
            for d in docs
            if d["kind"] == "Service" and d["spec"]["type"] == "NodePort"
        )
        self.assertEqual(external["spec"]["externalTrafficPolicy"], "Local")
        self.assertEqual(external["spec"]["ports"][0]["nodePort"], 30880)

    def test_disabling_shaping_removes_controller_and_privileges(self):
        docs = self.render(netem={"enabled": False})
        pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"][
            "spec"
        ]
        self.assertNotIn("initContainers", pod)
        self.assertEqual(len(pod["containers"]), 1)
        self.assertNotIn("NET_ADMIN", str(pod))

    def test_conflicting_listeners_are_rejected(self):
        base = {
            "name": "stream-a",
            "listenPort": 19000,
            "targetHost": "target",
            "targetPort": 20000,
        }
        for forwards in (
            [base, {**base, "name": "stream-b"}],
            [base, {**base, "listenPort": 19001}],
            [{**base, "listenPort": 18080}],
        ):
            with self.subTest(forwards=forwards):
                self.render(forwards=forwards, success=False)


if __name__ == "__main__":
    unittest.main()
