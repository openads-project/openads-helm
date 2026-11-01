import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


@unittest.skipUnless(shutil.which("helm"), "helm is required")
class ChartCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        charts = Path(__file__).resolve().parents[2]
        self.chart = self.root / "zenoh-router-netem"
        router = self.chart / "charts/zenoh-router"
        for source, destination in (
            (charts / "zenoh-router-netem", self.chart),
            (charts / "zenoh-router", router),
            (charts / "openadservice", router / "charts/openadservice"),
        ):
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("charts", "__pycache__"))

    def render_documents(self, service=None, netem=None):
        values = self.root / "values.yaml"
        values.write_text(yaml.safe_dump({
            "zenoh-router": {"openadservice": service or {}},
            "netem": netem or {},
        }))
        result = subprocess.run(
            ["helm", "template", "bridge", str(self.chart), "-f", str(values)],
            check=True, capture_output=True, text=True,
        )
        documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        identities = [(doc["kind"], doc["metadata"]["name"]) for doc in documents]
        self.assertEqual(len(identities), len(set(identities)))
        configmaps = {doc["metadata"]["name"] for doc in documents if doc["kind"] == "ConfigMap"}
        for deployment in (doc for doc in documents if doc["kind"] == "Deployment"):
            for volume in deployment["spec"]["template"]["spec"]["volumes"]:
                if "configMap" in volume:
                    self.assertIn(volume["configMap"]["name"], configmaps)
        return documents

    def render(self, ports):
        documents = self.render_documents({"ports": ports})
        deployments = [doc for doc in documents if doc["kind"] == "Deployment"]
        self.assertEqual(len(deployments), 1)
        deployment = deployments[0]
        containers = deployment["spec"]["template"]["spec"]["containers"]
        self.assertEqual(containers[1]["ports"], [{
            "name": "netem-http", "containerPort": 18080, "protocol": "TCP",
        }])
        return deployment, containers[0], documents

    def test_host_ports_use_recreate_and_preserve_protocols(self):
        deployment, router, documents = self.render([
            {"targetPort": 7447, "hostPort": 7448},
            {"targetPort": 7449, "hostPort": 7450, "protocol": "UDP"},
        ])
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        self.assertFalse(any(
            doc["kind"] == "Service" and doc["metadata"]["name"].endswith("-external")
            for doc in documents
        ))
        self.assertEqual(router["ports"], [
            {"containerPort": 7447, "hostPort": 7448, "protocol": "TCP"},
            {"containerPort": 7449, "hostPort": 7450, "protocol": "UDP"},
        ])

    def test_node_ports_keep_default_update_strategy(self):
        deployment, router, documents = self.render([
            {"targetPort": 7447, "nodePort": 30448},
        ])
        self.assertNotIn("strategy", deployment["spec"])
        self.assertEqual(router["ports"], [{"containerPort": 7447, "protocol": "TCP"}])
        services = [doc for doc in documents if doc["kind"] == "Service"]
        self.assertTrue(any(
            port.get("nodePort") == 30448
            for service in services for port in service["spec"]["ports"]
        ))

    def test_no_ports_keeps_default_update_strategy(self):
        deployment, router, _ = self.render([])
        self.assertNotIn("strategy", deployment["spec"])
        self.assertNotIn("ports", router)


    def test_prefixes_node_selection_dependencies_and_rmw_overrides(self):
        for prefix_dependencies in [True, False]:
            with self.subTest(prefix_dependencies=prefix_dependencies):
                documents = self.render_documents({
                    "namePrefix": "test",
                    "nodeSelector": {"kubernetes.io/hostname": "router-node"},
                    "dependsOn": [{"host": "upstream", "port": 7447}],
                    "dependsOnApplyNamePrefix": prefix_dependencies,
                    "rmw": {"zenoh": {"configOverrides": {"mode": "client"}}},
                    "env": {"ZENOH_CONFIG_OVERRIDE": "connect/timeout_ms=-1"},
                    "rosParamFileMountPath": "/params.yml",
                    "rosParamFileData": "name: {{ .Values.name }}",
                    "volumes": [{"mountPath": "/custom.txt", "data": "{{ .Values.name }}"}],
                })
                deployment = next(d for d in documents if d["kind"] == "Deployment")
                self.assertEqual(deployment["metadata"]["name"], "test-zenoh-router")
                pod = deployment["spec"]["template"]["spec"]
                self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "router-node"})
                host = "test-upstream" if prefix_dependencies else "upstream"
                self.assertIn(f"nc -z -w 1 {host} 7447", pod["initContainers"][0]["command"][-1])
                env = pod["containers"][0]["env"]
                overrides = [e["value"] for e in env if e["name"] == "ZENOH_CONFIG_OVERRIDE"]
                self.assertEqual(overrides, ['mode="client";connect/timeout_ms=-1'])
                services = [d for d in documents if d["kind"] == "Service"]
                self.assertTrue(all(
                    d["spec"]["selector"]["app.kubernetes.io/name"] == "test-zenoh-router"
                    for d in services
                ))
                inline = next(d for d in documents if "custom.txt" in d.get("data", {}))
                self.assertEqual(inline["data"]["custom.txt"].strip(), "test-zenoh-router")

    def test_instances_merge_overrides_and_keep_resources_consistent(self):
        documents = self.render_documents({
            "namePrefix": "test",
            "env": {"COMMON": "shared"},
            "instances": {
                "east": {
                    "nodeSelector": {"kubernetes.io/hostname": "east-node"},
                    "ports": [{"targetPort": 7447, "hostPort": 7448}],
                    "rmw": {"zenoh": {"configOverrides": {"mode": "client"}}},
                },
                "west": {
                    "nodeSelector": {"kubernetes.io/hostname": "west-node"},
                    "ports": [{"targetPort": 7447, "nodePort": 30448}],
                },
                "disabled": {"enable": False},
            },
        }, {"http": {"nodePort": None}})
        deployments = {d["metadata"]["name"]: d for d in documents if d["kind"] == "Deployment"}
        self.assertEqual(set(deployments), {"test-zenoh-router-east", "test-zenoh-router-west"})
        for key in ["east", "west"]:
            deployment = deployments[f"test-zenoh-router-{key}"]
            pod = deployment["spec"]["template"]["spec"]
            self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": f"{key}-node"})
            env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
            self.assertEqual(env["COMMON"], "shared")
            self.assertEqual("ZENOH_CONFIG_OVERRIDE" in env, key == "east")
            self.assertEqual("strategy" in deployment["spec"], key == "east")
        selectors = {
            d["spec"]["selector"]["app.kubernetes.io/name"]
            for d in documents if d["kind"] == "Service"
        }
        self.assertEqual(selectors, set(deployments))
        self.assertFalse(any("disabled" in d["metadata"]["name"] for d in documents))

    def test_mixed_ports_only_expose_node_port_entries(self):
        _, _, documents = self.render([
            {"targetPort": 7447, "hostPort": 7448},
            {"targetPort": 7449, "nodePort": 30449},
        ])
        external = next(d for d in documents if d["metadata"]["name"] == "zenoh-router-external")
        self.assertEqual(len(external["spec"]["ports"]), 1)
        self.assertEqual(external["spec"]["ports"][0]["nodePort"], 30449)
        self.assertEqual(external["spec"]["externalTrafficPolicy"], "Local")

    def test_router_container_matches_regular_chart(self):
        service = {
            "namePrefix": "test",
            "nodeSelector": {"kubernetes.io/hostname": "router-node"},
            "rmw": {"zenoh": {"configOverrides": {"mode": "client"}}},
            "env": {"ZENOH_CONFIG_OVERRIDE": "connect/timeout_ms=-1"},
            "dependsOn": [{"host": "upstream", "port": 7447}],
            "ports": [{"targetPort": 7447, "hostPort": 7448}],
        }
        netem_docs = self.render_documents(service)
        values = self.root / "regular.yaml"
        values.write_text(yaml.safe_dump({"openadservice": service}))
        result = subprocess.run(
            ["helm", "template", "bridge", str(self.chart / "charts/zenoh-router"), "-f", str(values)],
            check=True, capture_output=True, text=True,
        )
        regular_docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        netem = next(d for d in netem_docs if d["kind"] == "Deployment")
        regular = next(d for d in regular_docs if d["kind"] == "Deployment")
        self.assertEqual(netem["metadata"], regular["metadata"])
        self.assertEqual(netem["spec"]["strategy"], regular["spec"]["strategy"])
        netem_pod = netem["spec"]["template"]["spec"]
        regular_pod = regular["spec"]["template"]["spec"]
        self.assertEqual(netem_pod["containers"][0], regular_pod["containers"][0])
        self.assertEqual(netem_pod["initContainers"][:-1], regular_pod["initContainers"])
        self.assertEqual(netem_pod["volumes"][:-1], regular_pod["volumes"])

    def test_long_names_keep_netem_resources_valid(self):
        documents = self.render_documents({"namePrefix": "router-" * 12})
        netem_services = [
            d for d in documents
            if d["kind"] == "Service" and d["spec"].get("type") == "NodePort"
        ]
        self.assertEqual(len(netem_services), 1)
        self.assertLessEqual(len(netem_services[0]["metadata"]["name"]), 63)

    def test_disabled_netem_and_http_keep_router_resources(self):
        for options in [{"enabled": False}, {"http": {"enabled": False}}]:
            with self.subTest(options=options):
                documents = self.render_documents(netem=options)
                deployment = next(d for d in documents if d["kind"] == "Deployment")
                pod = deployment["spec"]["template"]["spec"]
                self.assertEqual(len(pod["containers"]), 1)
                self.assertFalse(any(
                    d["kind"] == "Service" and d["metadata"]["name"].endswith("-netem")
                    for d in documents
                ))
                self.assertEqual(
                    any(c["name"] == "netem-init" for c in pod.get("initContainers", [])),
                    options.get("enabled", True),
                )


if __name__ == "__main__":
    unittest.main()
