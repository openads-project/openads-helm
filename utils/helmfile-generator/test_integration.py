import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helmfile_generator import ComposeToHelmfileConverter, ConversionError


class ComposeFixtureIntegrationTest(unittest.TestCase):
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
                "    profiles: [planning]\n",
                encoding="utf-8",
            )
            env_file = root / ".env"
            env_file.write_text("PORT=8080\n", encoding="utf-8")
            output_dir = root / "generated"
            generator = ComposeToHelmfileConverter(
                compose_file=compose_file,
                profiles=["planning"],
                output_dir=output_dir,
                env_file=env_file,
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
