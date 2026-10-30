import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from generate_helmfile_from_compose import ComposeToHelmfileConverter, ConversionError


class ComposeFixtureIntegrationTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("docker"), "docker compose is required")
    def test_preserves_resolved_zenoh_topology_without_chart_map(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            compose_file = root / "compose.yaml"
            compose_file.write_text(
                'services:\n'
                '  planning.controller:\n'
                '    image: ghcr.io/example/controller:v1.0.0\n'
                '    depends_on: [drivers.zed-camera.mediamtx]\n'
                '    environment:\n'
                '      SERVICE_URL: rtsp://drivers.zed-camera.mediamtx:8554\n'
                '      ZENOH_CONFIG_OVERRIDE: >-\n'
                '        ${ZENOH_PEER:?};connect/endpoints=["tcp/localization.state:7447",'
                '"tcp/zenoh-router:7447","tcp/drivers.fka-dbw:7447"]\n'
                '  localization.state:\n'
                '    image: ghcr.io/example/state:v1.0.0\n'
                '    environment:\n'
                '      ZENOH_CONFIG_OVERRIDE: ${ZENOH_PEER:?}\n'
                '  zenoh-router:\n'
                '    image: ghcr.io/example/router:v1.0.0\n'
                '    environment:\n'
                '      ZENOH_CONFIG_OVERRIDE: routing/router/peers_failover_brokering=false\n'
                '  drivers.zed-camera.mediamtx:\n'
                '    image: ghcr.io/example/mediamtx:v1.0.0\n'
                '    ports: ["18554:8554/tcp"]\n'
            )
            (root / ".env").write_text(
                'ZENOH_PEER=\'mode="peer";listen/endpoints=["tcp/0.0.0.0:7447"];'
                'scouting/gossip/enabled=false;scouting/multicast/enabled=false\'\n'
            )
            generator = ComposeToHelmfileConverter(compose_file, [], root / "generated")
            with patch.object(generator, "_chart_defaults", return_value={}), patch("builtins.print") as output:
                generator.convert()
            controller = (root / "generated/controller/values.yaml.gotmpl").read_text()
            state = (root / "generated/state/values.yaml.gotmpl").read_text()
            router = (root / "generated/zenoh-router/values.yaml.gotmpl").read_text()
            self.assertIn('(tpl $prefixedName (list (env "DEPLOYMENT_PREFIX") "state"))', controller)
            self.assertNotIn('$zenohHosts', controller)
            self.assertNotIn("tcp/localization.state:7447", controller)
            self.assertIn("tcp/drivers.fka-dbw:7447", controller)
            self.assertIn("scouting/gossip/enabled: false", controller)
            self.assertNotIn("ZENOH_CONFIG_OVERRIDE:", controller)
            self.assertEqual(
                list((root / "generated").rglob("_helpers.tpl")),
                [root / "generated/templates/_helpers.tpl"],
            )
            self.assertIn("targetPort: 7447", state)
            self.assertIn("mode: peer", state)
            self.assertIn("routing/router/peers_failover_brokering: false", router)
            self.assertIn("host: mediamtx", controller)
            self.assertIn("port: 8554", controller)
            mediamtx = (root / "generated/mediamtx/values.yaml.gotmpl").read_text()
            self.assertIn("hostPort: 18554", mediamtx)
            self.assertIn("expose:\n  - targetPort: 8554\n    protocol: TCP", mediamtx)
            self.assertTrue(any("outside generated services" in str(call) for call in output.call_args_list))

    @unittest.skipUnless(shutil.which("docker"), "docker compose is required")
    def test_generates_generic_compose_fixture_without_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            compose_file = root / "compose.yaml"
            compose_file.write_text(
                "services:\n"
                "  api:\n"
                "    image: ghcr.io/example/api:v1.2.3\n"
                "    environment:\n"
                "      PORT: ${PORT:-8080}\n"
                "    expose: [8080]\n"
                "    ports: ['18080:8080', '30502:7502/udp']\n"
                "    profiles: [planning]\n",
                encoding="utf-8",
            )
            env_file = root / ".env"
            env_file.write_text("PORT=8080\n", encoding="utf-8")
            output_dir = root / "generated"
            chart_map = root / "chart-map.yaml"
            chart_map.write_text(
                "services:\n  api:\n    port_policies:\n      30502: nodePort\n",
                encoding="utf-8",
            )
            generator = ComposeToHelmfileConverter(
                compose_file=compose_file,
                profiles=["planning"],
                output_dir=output_dir,
                env_file=env_file,
                chart_map=chart_map,
            )

            with patch.object(
                generator,
                "_chart_defaults",
                return_value={
                    "name": "api",
                    "image": "ghcr.io/example/api:v1.2.3",
                    "env": {"PORT": "8080"},
                    "expose": [{"port": 8080, "targetPort": 8080, "protocol": "TCP"}],
                },
            ):
                generator.convert()

            self.assertTrue((output_dir / "helmfile.yaml").is_file())
            self.assertTrue((output_dir / "api/values.yaml.gotmpl").is_file())
            values = (output_dir / "api/values.yaml.gotmpl").read_text()
            self.assertIn("hostPort: 18080", values)
            self.assertIn("nodePort: 30502", values)
            services = (output_dir / "environments/services.yaml.gotmpl").read_text()
            self.assertIn('default "planning"', services)
            self.assertFalse(any(output_dir.rglob(".env")))

    @unittest.skipUnless(shutil.which("docker"), "docker compose is required")
    def test_refuses_to_export_compose_dotenv_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            compose_file = root / "compose.yaml"
            compose_file.write_text(
                "services:\n"
                "  api:\n"
                "    image: ghcr.io/example/api:v1.2.3\n"
                "    volumes: [.env:/run/project.env]\n",
                encoding="utf-8",
            )
            env_file = root / ".env"
            env_file.write_text("VALUE=private\n", encoding="utf-8")
            generator = ComposeToHelmfileConverter(
                compose_file=compose_file,
                profiles=[],
                output_dir=root / "generated",
                env_file=env_file,
            )

            with (
                patch.object(generator, "_chart_defaults", return_value={}),
                self.assertRaisesRegex(ConversionError, "environment file"),
            ):
                generator.convert()


if __name__ == "__main__":
    unittest.main()
