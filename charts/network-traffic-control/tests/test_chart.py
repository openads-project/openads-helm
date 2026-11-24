import subprocess
import unittest
from pathlib import Path

import yaml


CHART = Path(__file__).resolve().parents[1]


class TestControlService(unittest.TestCase):
    def render(self, *overrides):
        rendered = subprocess.check_output(
            ["helm", "template", "shaper", str(CHART), "-f", str(CHART / "tests" / "values.yaml"),
             *overrides],
            text=True,
        )
        return {(doc["kind"], doc["metadata"]["name"]): doc["spec"]
                for doc in yaml.safe_load_all(rendered) if doc and "spec" in doc}

    def test_cluster_ip_points_directly_to_controller(self):
        resources = self.render("--set", "http.nodePort=null")
        self.assertEqual(set(resources), {("Deployment", "shaper"), ("Service", "shaper")})
        controller = resources["Deployment", "shaper"]["template"]
        service = resources["Service", "shaper"]
        self.assertTrue(controller["spec"]["hostNetwork"])
        self.assertEqual(service["selector"], controller["metadata"]["labels"])
        self.assertNotIn("nodePort", service["ports"][0])

    def test_node_port_points_to_pod_network_relay(self):
        resources = self.render("--set", "http.port=18081")
        controller = resources["Deployment", "shaper"]["template"]
        relay = resources["Deployment", "shaper-http"]["template"]
        external = resources["Service", "shaper"]
        internal = resources["Service", "shaper-local"]
        self.assertTrue(controller["spec"]["hostNetwork"])
        self.assertFalse(relay["spec"].get("hostNetwork", False))
        self.assertEqual(relay["spec"]["nodeSelector"], controller["spec"]["nodeSelector"])
        self.assertEqual(external["selector"], relay["metadata"]["labels"])
        self.assertEqual(internal["selector"], controller["metadata"]["labels"])
        self.assertEqual(external["ports"][0]["nodePort"], 30880)
        self.assertEqual(internal["ports"][0]["port"], 18081)
        container = relay["spec"]["containers"][0]
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertEqual(container["args"], [
            "TCP4-LISTEN:18081,reuseaddr,fork", "TCP4:shaper-local:18081,connect-timeout=2",
        ])
