import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_environment import main


class CiEnvironmentTest(unittest.TestCase):
    def test_exports_profiles_overrides_and_generic_host_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            compose_file = root / "compose.yaml"
            compose_file.write_text("services: {}\n", encoding="utf-8")
            (root / ".env").write_text(
                "COMPOSE_PROFILES=default\nREQUIRED_VALUE=value\n",
                encoding="utf-8",
            )
            chart_map = root / "chart-map.yaml"
            chart_map.write_text(
                "deployment:\n"
                "  name_prefix_env: DEPLOYMENT_PREFIX\n"
                "  host_path_root_env: PROJECT_ROOT\n",
                encoding="utf-8",
            )
            github_env = root / "github-env"

            with patch(
                "sys.argv",
                [
                    "ci_environment.py",
                    "--compose-file",
                    str(compose_file),
                    "--chart-map",
                    str(chart_map),
                    "--profiles",
                    "traffic,planning",
                    "--workspace",
                    str(root),
                    "--github-env",
                    str(github_env),
                ],
            ):
                main()

            exported = github_env.read_text(encoding="utf-8")
            self.assertIn("COMPOSE_PROFILES<<", exported)
            self.assertIn("traffic,planning", exported)
            self.assertIn("REQUIRED_VALUE<<", exported)
            self.assertIn("DEPLOYMENT_PREFIX<<", exported)
            self.assertIn("\nci\n", exported)
            self.assertIn("PROJECT_ROOT<<", exported)

    def test_rejects_ci_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            compose_file = root / "compose.yaml"
            compose_file.write_text("services: {}\n", encoding="utf-8")
            (root / ".env").write_text("GITHUB_TOKEN=unsafe\n", encoding="utf-8")

            with (
                patch(
                    "sys.argv",
                    [
                        "ci_environment.py",
                        "--compose-file",
                        str(compose_file),
                        "--workspace",
                        str(root),
                        "--github-env",
                        str(root / "github-env"),
                    ],
                ),
                self.assertRaisesRegex(ValueError, "refusing to override"),
            ):
                main()


if __name__ == "__main__":
    unittest.main()
