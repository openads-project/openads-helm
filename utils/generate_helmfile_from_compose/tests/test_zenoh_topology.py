import copy
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from generate_helmfile_from_compose import (
    ChartRef, ComposeToHelmfileConverter, ConversionError, ConvertedService,
)


class ZenohTopologyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.converter = ComposeToHelmfileConverter(
            self.root / "compose.yml", [], self.root / "output",
        )

    def convert_service(self, override, defaults=None):
        with (
            patch.object(self.converter, "_chart_ref", return_value=ChartRef("local", "1")),
            patch.object(self.converter, "_chart_defaults", return_value=defaults or {}),
        ):
            return self.converter._convert_service("control.controller", {
                "image": "example.test/controller:1",
                "environment": {"ZENOH_CONFIG_OVERRIDE": override},
            })

    def test_fixed_listeners_preserve_settings_and_merge_chart_exposure(self):
        override = (
            'mode="peer";listen/endpoints=["tcp/0.0.0.0:7447","tcp/[::]:7447",'
            '"udp/0.0.0.0:7448"];transport/shared_memory/enabled=true'
        )
        service = self.convert_service(override, {"expose": [{"targetPort": 8080}]})
        self.assertEqual(service.values["rmw"]["zenoh"]["configOverrides"], self.converter._zenoh_config(override))
        self.assertNotIn("ZENOH_CONFIG_OVERRIDE", service.values.get("env", {}))
        self.assertEqual(service.values["expose"], [
            {"targetPort": 8080},
            {"targetPort": 7447, "protocol": "TCP"},
            {"targetPort": 7448, "protocol": "UDP"},
        ])

    def test_ephemeral_and_loopback_listeners_are_not_exposed(self):
        override = (
            'mode="peer";listen/endpoints=["tcp/0.0.0.0:0","tcp/localhost:7447",'
            '"tcp/127.0.0.1:7448","tcp/[::1]:7449"];'
            'scouting/multicast/enabled=true;scouting/multicast/interface="lo"'
        )
        service = self.convert_service(override)
        self.assertNotIn("expose", service.values)
        self.assertEqual(service.values["rmw"]["zenoh"]["configOverrides"], self.converter._zenoh_config(override))
        self.assertNotIn("ZENOH_CONFIG_OVERRIDE", service.values.get("env", {}))

    def test_listener_port_is_added_when_chart_exposes_a_different_service_port(self):
        service = self.convert_service(
            'listen/endpoints=["tcp/0.0.0.0:7447"]',
            {"expose": [{"port": 17447, "targetPort": 7447}]},
        )
        self.assertEqual(service.values["expose"], [
            {"port": 17447, "targetPort": 7447},
            {"targetPort": 7447, "protocol": "TCP"},
        ])

    def test_mode_specific_listeners_and_existing_listener_are_preserved(self):
        service = self.convert_service(
            'mode="peer";listen/endpoints={"peer":["tcp/0.0.0.0:7447"],'
            '"client":["tcp/0.0.0.0:9999"]}',
            {"expose": [{"targetPort": 7447}]},
        )
        self.assertNotIn("expose", service.values)

    def test_override_splitter_preserves_endpoint_parameters_and_duplicate_settings(self):
        settings = self.converter._zenoh_settings(
            'mode="client";mode="peer";connect/endpoints=['
            '"tcp/host:7447#retry_period_init_ms=100;retry_period_max_ms=1000"]'
        )
        self.assertEqual(settings[:2], [("mode", '"client"'), ("mode", '"peer"')])
        self.assertIn(";retry_period_max_ms=1000", settings[2][1])

    def test_invalid_endpoint_configuration_fails_conversion(self):
        for override in (
            'listen/endpoints=[broken]', 'mode="unterminated', 'invalid-setting',
            'listen/endpoints=[42]',
        ):
            with self.subTest(override=override), self.assertRaises(ConversionError):
                self.convert_service(override)

    def test_external_endpoints_are_reported_and_left_unchanged(self):
        override = 'connect/endpoints=["tcp/drivers.fka-dbw:7447","tcp/[2001:db8::1]:7447"]'
        service = self.convert_service(override)
        statuses = self.converter._translate_zenoh_topology([service])
        self.assertEqual(service.values["rmw"]["zenoh"]["configOverrides"], self.converter._zenoh_config(override))
        self.assertNotIn("ZENOH_CONFIG_OVERRIDE", service.values.get("env", {}))
        self.assertEqual(len(statuses), 2)
        self.assertTrue(all("outside generated services" in status[3] for status in statuses))

    def test_ambiguous_instances_are_rejected(self):
        source = self.convert_service('connect/endpoints=["tcp/localization.target:7447"]')
        target = ConvertedService(
            "localization.target", "target", ChartRef("local", "1"), "target",
            values={"instances": {"one": {}, "two": {}}},
        )
        with (
            patch.object(self.converter, "_chart_defaults", return_value={}),
            self.assertRaisesRegex(ConversionError, "uses chart instances"),
        ):
            self.converter._translate_zenoh_topology([source, target])

    @unittest.skipUnless(shutil.which("helmfile") and shutil.which("helm"), "helmfile and helm are required")
    def test_rendered_endpoints_match_chart_service_names_for_runtime_prefixes(self):
        chart = Path(__file__).resolve().parents[3] / "charts/openadservice"
        source = self.convert_service(
            'mode="peer";scouting/gossip/enabled=false;connect/endpoints=['
            '"tcp/localization.target:7447#retry_period_init_ms=100;retry_period_max_ms=1000",'
            '"tcp/external.example:7447"]'
        )
        source.values["env"] = {
            "LOG_LEVEL": "info",
            "RTSP_BASE_URL": "rtsp://user:pass@localization.target:8554/front%20left?stream=low",
            "EXTERNAL_URL": "rtsp://external.example:8554/camera",
            "TRITON_SERVER_URL": "triton-server:8001",
            "TARGET_HOST": "localization.target",
            "TARGET_ADDRESS": "localization.target:8001",
        }
        target = ConvertedService(
            "localization.target", "target", ChartRef("local", "1"), "target",
            values={"name": "Custom.Target", "expose": [{"targetPort": 7447}]},
        )
        values_path = self.root / source.values_dir / "values.yaml.gotmpl"
        values_path.parent.mkdir(parents=True)
        (self.root / "templates").mkdir()
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "templates/_helpers.tpl",
            self.root / "templates/_helpers.tpl",
        )
        (self.root / "helmfile.yaml").write_text(yaml.safe_dump({"releases": [{
            "name": "check", "chart": str(chart), "values": [str(values_path)],
        }]}))
        cases = [(prefix, "Custom.Target") for prefix in (
            "", "test", "TEST_PREFIX", "custom", "custom-target", "prefix-" * 12,
        )] + [
            ("", "long-name-" * 10),
            ("test", "long-name-" * 10),
            ("--Fleet_Name.--", "Odd/Node_Name.."),
        ]
        for prefix, name in cases:
            with self.subTest(prefix=prefix, name=name):
                rendered_source = copy.deepcopy(source)
                target.values["name"] = name
                with patch.object(self.converter, "_chart_defaults", return_value={}):
                    self.converter._translate_zenoh_topology([rendered_source, target])
                    self.converter._translate_service_urls([rendered_source, target])
                rendered_values = self.converter._render_values(rendered_source)
                values_path.write_text(rendered_values)
                self.assertTrue(rendered_values.startswith('{{- $prefixedName := readFile "'))
                self.assertLess(rendered_values.index("\n  env:\n"), rendered_values.index("\n  rmw:\n"))
                self.assertIn(' -}}\nopenadservice:\n', rendered_values)
                environment = {**os.environ, "DEPLOYMENT_PREFIX": prefix, "NODE": ""}
                subprocess.run([
                    "helmfile", "--file", str(self.root / "helmfile.yaml"), "--skip-deps",
                    "write-values", "--output-file-template", str(self.root / "rendered.yaml"),
                ], env=environment, text=True, capture_output=True, check=True)
                values = yaml.safe_load((self.root / "rendered.yaml").read_text())["openadservice"]
                result = subprocess.run([
                    "helm", "template", "target", str(chart), "-f", "-",
                ], input=yaml.safe_dump({**target.values, "namePrefix": prefix}),
                    text=True, capture_output=True, check=True)
                target_service = next(
                    doc for doc in yaml.safe_load_all(result.stdout)
                    if doc and doc["kind"] == "Service"
                )
                self.assertEqual(values["env"]["RTSP_BASE_URL"],
                    f'rtsp://user:pass@{target_service["metadata"]["name"]}:8554/front%20left?stream=low')
                self.assertEqual(values["env"]["EXTERNAL_URL"], "rtsp://external.example:8554/camera")
                self.assertEqual(values["env"]["TRITON_SERVER_URL"], "triton-server:8001")
                self.assertEqual(values["env"]["TARGET_HOST"], target_service["metadata"]["name"])
                self.assertEqual(values["env"]["TARGET_ADDRESS"], f'{target_service["metadata"]["name"]}:8001')
                settings = values["rmw"]["zenoh"]["configOverrides"]
                endpoints = settings["connect/endpoints"]
                self.assertEqual(endpoints, [
                    f'tcp/{target_service["metadata"]["name"]}:7447#retry_period_init_ms=100;retry_period_max_ms=1000',
                    "tcp/external.example:7447",
                ])
                self.assertEqual(settings["mode"], "peer")
                self.assertIs(settings["scouting/gossip/enabled"], False)
                source_result = subprocess.run([
                    "helm", "template", "controller", str(chart), "-f", "-",
                ], input=yaml.safe_dump(values), text=True, capture_output=True, check=True)
                deployment = next(
                    doc for doc in yaml.safe_load_all(source_result.stdout)
                    if doc and doc["kind"] == "Deployment"
                )
                container = deployment["spec"]["template"]["spec"]["containers"][0]
                overrides = next(item["value"] for item in container["env"] if item["name"] == "ZENOH_CONFIG_OVERRIDE")
                self.assertEqual(self.converter._zenoh_config(overrides), settings)
