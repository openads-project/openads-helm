import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helmfile_generator import ChartRef, ComposeToHelmfileConverter, ConvertedService


class HostEnvironmentRenderingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        chart_map = root / "chart-map.yaml"
        chart_map.write_text(
            'values_root: openadservice\nparameter_file:\n  yaml_marker: ros__parameters\n  value_key: rosParamFileData\n  mount_path_value_key: rosParamFileMountPath\n  output_filename: params.yml\ndeployment:\n  name_prefix_env: DEPLOYMENT_PREFIX\n  node_env: NODE\n  host_path_root_env: OPENADSIM_PATH\n'
            'services:\n  carla-server:\n    chart: oci://example.test/carla-server\n    version: 1.0.0\n    dependency_port_env: CARLA_PORT\n'
            '  carla-ros-bridge:\n    chart: oci://example.test/carla-ros-bridge\n    version: 1.0.0\n'
        )
        self.converter = ComposeToHelmfileConverter(
            compose_file=root / "compose.yml",
            profiles=[],
            output_dir=root / "output",
            env_file=root / ".env",
            chart_map=chart_map,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_empty_compose_default_reads_runtime_environment(self) -> None:
        expression = self.converter._compose_expression("${SPAWN_POINT:-}")

        self.assertEqual(expression, '(default "" (env "SPAWN_POINT"))')

    def test_unconfigured_project_has_no_deployment_prefix(self) -> None:
        root = Path(self.temp_dir.name)
        generic = ComposeToHelmfileConverter(
            compose_file=root / "compose.yml", profiles=[],
            output_dir=root / "plain", env_file=root / ".env",
        )
        service = ConvertedService(
            name="api", release_name="api",
            chart=ChartRef("oci://example.test/api", "1.0.0"),
            values_dir="api",
        )
        self.assertIn("- name: api\n", generic._render_helmfile([service]))
        self.assertNotIn("namePrefix", generic._render_values(service))
        self.assertEqual(
            generic._chart_ref("api", "ghcr.io/example/api:v1.2.3").version,
            "1.2.3",
        )

    def test_dependency_port_rule_applies_to_arbitrary_service(self) -> None:
        self.converter.chart_map["services"]["api"] = {"dependency_port_env": "API_PORT"}
        self.converter.raw_services["api"] = {
            "environment": {"API_PORT": "${API_PORT:-8080}"}
        }
        self.assertEqual(
            self.converter._depends_on({"depends_on": ["api"]}),
            [{"host": "api", "port": "${API_PORT:-8080}"}],
        )

    def test_compose_default_is_preserved(self) -> None:
        expression = self.converter._compose_expression("${MAP:-campus}")

        self.assertEqual(expression, '(default "campus" (env "MAP"))')

    def test_repeated_host_variable_reads_runtime_environment(self) -> None:
        expression = self.converter._compose_expression(
            "${CUSTOM_OPENDRIVE:-${MAP:-}}"
        )

        self.assertEqual(
            expression,
            '(default (default "" (env "MAP")) (env "CUSTOM_OPENDRIVE"))',
        )

    def test_deployment_environment_and_release_prefix_are_runtime_values(self) -> None:
        service = ConvertedService(
            name="carla-server",
            release_name="carla-server",
            chart=ChartRef("oci://example.test/carla-server", "1.0.0"),
            values_dir="carla-simulation/carla-server",
            profiles=("carla",),
        )
        helmfile = self.converter._render_helmfile([service])
        self.assertIn(
            '{{ printf "%s-carla-server" (env "DEPLOYMENT_PREFIX") | trimPrefix "-" }}',
            helmfile,
        )
        self.assertIn("values: [carla-simulation/carla-server/values.yaml.gotmpl]", helmfile)
        self.assertIn("environments/services.yaml.gotmpl", helmfile)
        self.assertIn("condition: serviceEnabled.carla-server.enabled", helmfile)
        selection = self.converter._render_services([service])
        self.assertIn('(has "carla" $selectedProfiles)', selection)
        values = self.converter._render_values(service)
        self.assertIn('namePrefix: {{ env "DEPLOYMENT_PREFIX" | quote }}', values)
        self.assertIn('{{- if env "NODE" }}', values)
        self.assertIn('kubernetes.io/hostname: {{ env "NODE" | quote }}', values)

    def test_empty_service_values_still_render_as_a_mapping(self) -> None:
        service = ConvertedService(
            name="zenoh-router",
            release_name="zenoh-router",
            chart=ChartRef("oci://example.test/zenoh-router", "1.0.0"),
            values_dir="openadstack/essentials/zenoh-router",
        )

        values = self.converter._render_values(service)
        self.assertTrue(values.startswith("openadservice:\n  namePrefix:"))
        self.assertNotIn("openadservice: {}", values)

    def test_colliding_compose_names_keep_their_prefixes(self) -> None:
        self.converter.raw_services = {
            "carla.simulation-adapter": {"profiles": ["carla"]},
            "sumo.simulation-adapter": {"profiles": ["sumo"]},
            "control.ackermann-trajectory-control": {"profiles": ["planning"]},
        }
        self.assertEqual(
            self.converter._release_name("carla.simulation-adapter"),
            "carla-simulation-adapter",
        )
        self.assertEqual(
            self.converter._release_name("sumo.simulation-adapter"),
            "sumo-simulation-adapter",
        )
        self.assertEqual(
            self.converter._release_name("control.ackermann-trajectory-control"),
            "ackermann-trajectory-control",
        )

    def test_carla_host_env_is_independent_of_dependency(self) -> None:
        service = ConvertedService(
            name="carla-client",
            release_name="carla-client",
            chart=ChartRef("oci://example.test/carla-client", "1.0.0"),
            values_dir="carla-simulation/carla-client",
            values={
                "dependsOn": [{"host": "carla-server", "port": 2000}],
            },
            runtime_environment={"CARLA_HOST": "${CARLA_HOST:-carla-server}"},
        )
        values = self.converter._render_values(service)
        self.assertIn("host: carla-server", values)
        self.assertIn("port: 2000", values)
        self.assertNotIn("prefix:", values)
        self.assertIn(
            'CARLA_HOST: {{ (default "carla-server" (env "CARLA_HOST")) | quote }}',
            values,
        )
        self.assertNotIn("envHostPrefixKeys", values)

    def test_bridge_host_runtime_override_survives_chart_defaults(self) -> None:
        service_name = "carla-ros-bridge"
        self.converter.raw_services[service_name] = {
            "environment": {
                "HOST": "${CARLA_HOST:-carla-server}",
                "PORT": "${CARLA_PORT:-2000}",
            }
        }
        resolved = {
            "image": "example.test/bridge:1",
            "environment": {"HOST": "carla-server", "PORT": "2000"},
        }
        defaults = {
            "name": service_name,
            "image": resolved["image"],
            "env": resolved["environment"],
        }
        with (
            patch.object(
                self.converter, "_chart_ref",
                return_value=ChartRef("oci://example.test/bridge", "1.0.0"),
            ),
            patch.object(self.converter, "_chart_defaults", return_value=defaults),
        ):
            converted = self.converter._convert_service(service_name, resolved)

        values = self.converter._render_values(converted)
        self.assertIn('HOST: {{ (default "carla-server" (env "CARLA_HOST")) | quote }}', values)
        self.assertIn('PORT: {{ (default "2000" (env "CARLA_PORT")) | quote }}', values)

    def test_carla_dependency_uses_compose_service_name(self) -> None:
        self.converter.raw_services = {"carla-server": {"image": "example.test/carla"}}
        with (
            patch.object(
                self.converter,
                "_chart_ref",
                return_value=ChartRef("oci://example.test/carla-server", "1.0.0"),
            ),
            patch.object(self.converter, "_chart_defaults", return_value={"expose": []}),
            patch.object(
                self.converter,
                "_ports",
                return_value=([], [{"targetPort": 2000, "protocol": "TCP"}]),
            ),
        ):
            checks = self.converter._depends_on({"depends_on": ["carla-server"]})

        self.assertEqual(checks, [{"host": "carla-server", "port": 2000}])

    def test_carla_dependency_port_reads_runtime_carla_port(self) -> None:
        self.converter.raw_services["carla-server"] = {
            "environment": {"CARLA_PORT": "${CARLA_PORT:-2000}"}
        }
        checks = self.converter._depends_on({"depends_on": ["carla-server"]})

        self.assertEqual(
            checks, [{"host": "carla-server", "port": "${CARLA_PORT:-2000}"}]
        )

    def test_environment_keeps_compose_source_order(self) -> None:
        self.converter.raw_services["bridge"] = {
            "environment": {
                "USE_SIM_TIME": "${USE_SIM_TIME:-true}",
                "HOST": "${CARLA_HOST:-carla-server}",
                "PORT": "${CARLA_PORT:-2000}",
            }
        }
        resolved = {"environment": {
            "HOST": "carla-server", "PORT": "2000", "USE_SIM_TIME": "true",
        }}
        defaults = {"env": {"HOST": "carla-server", "PORT": "2000"}}

        environment = self.converter._environment("bridge", resolved, defaults)

        self.assertEqual(list(environment), ["USE_SIM_TIME", "HOST", "PORT"])

    def test_compose_source_override_tag_keeps_replacement_profiles(self) -> None:
        self.converter.compose_file.write_text(
            "services:\n"
            "  carla-server:\n"
            "    profiles: [carla]\n"
            "  carla-server-offscreen:\n"
            "    extends:\n"
            "      service: carla-server\n"
            "    profiles: !override [donotuse]\n",
            encoding="utf-8",
        )

        services = self.converter._discover_raw_services()

        self.assertEqual(services["carla-server-offscreen"]["profiles"], ["donotuse"])

    def test_optional_opendrive_mount_stays_runtime_configurable(self) -> None:
        root = self.converter.compose_dir
        source_file = root / "carla-simulation/carla_ros_bridge/docker-compose.yml"
        source_file.parent.mkdir(parents=True)
        source_file.touch()
        map_file = root / "scenarios/campus.xodr"
        map_file.parent.mkdir()
        map_file.write_text("map", encoding="utf-8")
        service_name = "carla-ros-bridge"
        self.converter.raw_services[service_name] = {
            "volumes": [
                "${CUSTOM_OPENDRIVE:+../../}"
                "${CUSTOM_OPENDRIVE:-/dev/null:/dev/null}"
                "${CUSTOM_OPENDRIVE:+:/opendrive.xodr}"
            ]
        }
        self.converter.raw_service_sources[service_name] = source_file
        resolved_service = {
            "image": "example.test/bridge:1",
            "volumes": [{
                "type": "bind", "source": str(map_file), "target": "/opendrive.xodr",
            }],
        }
        with patch.object(
            self.converter,
            "_chart_defaults",
            return_value={"name": service_name, "image": resolved_service["image"]},
        ):
            converted = self.converter._convert_service(service_name, resolved_service)

        self.assertEqual(len(converted.values["volumes"]), 1)
        self.assertNotIn("opendrive.xodr", converted.config_sources)
        values = self.converter._render_values(converted)
        self.assertIn("mountPath: /opendrive.xodr", values)
        self.assertIn('env "CUSTOM_OPENDRIVE"', values)
        self.assertIn('requiredEnv "OPENADSIM_PATH"', values)

    def test_optional_bind_pattern_applies_to_another_service(self) -> None:
        source_file = self.converter.compose_dir / "openadstack/localization/docker-compose.yml"
        source_file.parent.mkdir(parents=True)
        source_file.touch()
        self.converter.raw_services["map-server"] = {
            "volumes": [
                "${CUSTOM_LANELET:+../../}"
                "${CUSTOM_LANELET:-/dev/null:/dev/null}"
                "${CUSTOM_LANELET:+:/lanelet/lanelet.osm}"
            ]
        }
        self.converter.raw_service_sources["map-server"] = source_file

        volumes = self.converter._runtime_volumes("map-server", {"volumes": []})

        self.assertEqual(len(volumes), 1)
        self.assertEqual(volumes[0]["mountPath"], "/lanelet/lanelet.osm")
        self.assertIn('env "CUSTOM_LANELET"', volumes[0]["hostPath"])

    def test_chart_map_volume_keeps_other_converted_mounts(self) -> None:
        service_name = "map-server"
        source_file = self.converter.compose_dir / "maps/docker-compose.yml"
        source_file.parent.mkdir()
        source_file.touch()
        self.converter.raw_services[service_name] = {
            "volumes": [
                "${CUSTOM_LANELET:+../}"
                "${CUSTOM_LANELET:-/dev/null:/dev/null}"
                "${CUSTOM_LANELET:+:/lanelet/lanelet.osm}"
            ]
        }
        self.converter.raw_service_sources[service_name] = source_file
        self.converter.chart_map["services"][service_name] = {
            "chart": "oci://example.test/map-server",
            "version": "1.0.0",
            "values": {"volumes": [{
                "mountPath": "/data/maps", "hostPath": ".runtime/maps",
            }]},
        }
        with patch.object(
            self.converter, "_chart_defaults",
            return_value={"name": service_name, "image": "example.test/map:1"},
        ):
            converted = self.converter._convert_service(
                service_name, {"image": "example.test/map:1", "volumes": []}
            )

        self.assertEqual(
            {volume["mountPath"] for volume in converted.values["volumes"]},
            {"/data/maps", "/lanelet/lanelet.osm"},
        )
        self.assertIn('requiredEnv "OPENADSIM_PATH"', self.converter._render_values(converted))

    def test_variable_directory_bind_uses_runtime_host_root(self) -> None:
        root = self.converter.compose_dir
        source_file = root / "openadstack/monitoring/docker-compose.yml"
        source_file.parent.mkdir(parents=True)
        source_file.touch()
        source_dir = root / "openadstack/monitoring/volume"
        source_dir.mkdir()
        self.converter.raw_services["bag-recorder"] = {
            "volumes": [
                "../../${BAG_DIRECTORY:-openadstack/monitoring/volume}:/tmp/rosbags"
            ]
        }
        self.converter.raw_service_sources["bag-recorder"] = source_file
        resolved_service = {
            "image": "example.test/bag:1",
            "volumes": [{
                "type": "bind", "source": str(source_dir), "target": "/tmp/rosbags",
            }],
        }
        with (
            patch.object(
                self.converter, "_chart_ref",
                return_value=ChartRef("oci://example.test/bag", "1.0.0"),
            ),
            patch.object(
                self.converter, "_chart_defaults",
                return_value={"name": "bag-recorder", "image": resolved_service["image"]},
            ),
        ):
            converted = self.converter._convert_service("bag-recorder", resolved_service)

        values = self.converter._render_values(converted)
        self.assertIn("mountPath: /tmp/rosbags", values)
        self.assertIn('env "BAG_DIRECTORY"', values)
        self.assertIn('requiredEnv "OPENADSIM_PATH"', values)

    def test_fixed_binary_file_is_copied_beside_generated_values(self) -> None:
        root = self.converter.compose_dir
        source = root / "assets/model.bin"
        source.parent.mkdir()
        source.write_bytes(b"\x00\xff" * 10)
        volumes, param_source, param_filename, config_sources, file_sources, directories = (
            self.converter._volumes(
                "binary-service",
                {"volumes": [{
                    "type": "bind", "source": str(source), "target": "/model.bin",
                }]},
                {},
                "assets/binary-service",
            )
        )
        self.assertIsNone(param_source)
        self.assertIsNone(param_filename)
        self.assertFalse(config_sources)
        self.assertEqual(file_sources, {"model.bin": source})
        self.assertFalse(directories)
        self.assertIn('requiredEnv "OPENADSIM_PATH"', volumes[0]["hostPath"])
        service = ConvertedService(
            name="binary-service",
            release_name="binary-service",
            chart=ChartRef("oci://example.test/binary-service", "1.0.0"),
            values_dir="assets/binary-service",
            values={"volumes": volumes},
            file_sources=file_sources,
        )
        self.converter.compose_file.write_text("services: {}\n", encoding="utf-8")
        self.converter.env_file.write_text("", encoding="utf-8")
        with (
            patch.object(self.converter, "_discover_raw_services", return_value={}),
            patch.object(
                self.converter, "_resolve_compose",
                return_value={"services": {"binary-service": {}}},
            ),
            patch.object(self.converter, "_convert_service", return_value=service),
        ):
            self.converter.convert()

        copied = self.converter.output_dir / "assets/binary-service/model.bin"
        self.assertEqual(copied.read_bytes(), source.read_bytes())

    def test_directory_copy_replaces_stale_generated_files(self) -> None:
        source = self.converter.compose_dir / "assets/objects"
        source.mkdir(parents=True)
        (source / "objects.json").write_text("{}", encoding="utf-8")
        (source / ".gitignore").write_text("*.json\n", encoding="utf-8")
        volumes, _, _, _, files, directories = self.converter._volumes(
            "object-service",
            {"volumes": [{
                "type": "bind", "source": str(source), "target": "/config/objects",
            }]},
            {},
            "assets/object-service",
        )
        self.assertFalse(files)
        self.assertEqual(directories, {"objects": source})
        self.assertIn("assets/object-service/objects", volumes[0]["hostPath"])
        service = ConvertedService(
            name="object-service",
            release_name="object-service",
            chart=ChartRef("oci://example.test/object-service", "1.0.0"),
            values_dir="assets/object-service",
            values={"volumes": volumes},
            directory_sources=directories,
        )
        self.converter.compose_file.write_text("services: {}\n", encoding="utf-8")
        self.converter.env_file.write_text("", encoding="utf-8")
        destination = self.converter.output_dir / "assets/object-service/objects"
        destination.mkdir(parents=True)
        (destination / "stale.json").write_text("old", encoding="utf-8")
        with (
            patch.object(self.converter, "_discover_raw_services", return_value={}),
            patch.object(
                self.converter, "_resolve_compose",
                return_value={"services": {"object-service": {}}},
            ),
            patch.object(self.converter, "_convert_service", return_value=service),
        ):
            self.converter.convert()
        self.assertTrue(
            (self.converter.output_dir / "environments/services.yaml.gotmpl").is_file()
        )
        self.assertFalse(
            (self.converter.output_dir / "environments/overrides.yaml.gotmpl").exists()
        )
        self.assertEqual((destination / "objects.json").read_text(), "{}")
        self.assertFalse((destination / ".gitignore").exists())
        self.assertFalse((destination / "stale.json").exists())

    def test_host_path_and_omit_override_directory_copy(self) -> None:
        source = self.converter.compose_dir / "shared"
        source.mkdir()
        self.converter.chart_map["volume_policies"] = {
            "shared-service": {"/keep": "hostPath", "/skip": "omit"}
        }
        volumes, _, _, _, _, directories = self.converter._volumes(
            "shared-service",
            {"volumes": [
                {"type": "bind", "source": str(source), "target": "/keep"},
                {"type": "bind", "source": str(source), "target": "/skip"},
            ]},
            {},
            "assets/shared-service",
        )
        self.assertEqual([volume["mountPath"] for volume in volumes], ["/keep"])
        self.assertIn('requiredEnv "OPENADSIM_PATH"', volumes[0]["hostPath"])
        self.assertNotIn("assets/shared-service", volumes[0]["hostPath"])
        self.assertFalse(directories)

    def test_numeric_looking_chart_version_stays_a_string(self) -> None:
        service = ConvertedService(
            name="triton-server",
            release_name="triton-server",
            chart=ChartRef("oci://example.test/triton-server", "26.05"),
            values_dir="openadstack/essentials/triton-server",
        )
        self.assertIn("version: '26.05'", self.converter._render_helmfile([service]))


if __name__ == "__main__":
    unittest.main()
