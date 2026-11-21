"""exercise reconciliation, validation, rollback and crash recovery with fake sysfs."""

import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "files/controller.py"
spec = importlib.util.spec_from_file_location("controller", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
UID = "12345678-1234-1234-1234-123456789abc"


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cpu, self.cg = self.root / "cpu", self.root / "cgroup"
        self.cpu.mkdir()
        self.cg.mkdir()
        (self.cpu / "online").write_text("0-3")
        (self.cg / "cgroup.controllers").write_text("cpuset cpu memory")
        for number in range(4):
            d = self.cpu / f"cpu{number}/topology"
            d.mkdir(parents=True)
            (d / "thread_siblings_list").write_text("0,2" if number % 2 == 0 else "1,3")
            policy = self.cpu / f"cpufreq/policy{number}"
            policy.mkdir(parents=True)
            for f, value in {
                "related_cpus": str(number),
                "cpuinfo_min_freq": "500000",
                "cpuinfo_max_freq": "4000000",
                "scaling_min_freq": "3000000",
                "scaling_max_freq": "4000000",
            }.items():
                (policy / f).write_text(value)
            (d.parent / "cpufreq").symlink_to(policy)
        self.pod = {
            "metadata": {"uid": UID, "name": "planner", "labels": {"pool": "ads"}},
            "status": {"qosClass": "BestEffort", "phase": "Running"},
        }
        self.path = self.cg / module.pod_cgroup(self.pod)
        self.path.mkdir(parents=True)
        (self.path.parent / "cpuset.cpus.effective").write_text("0-3")
        (self.path / "cpuset.cpus").write_text("\n")
        (self.path / "cpuset.cpus.effective").write_text("0,2")
        self.controller = module.Controller(self.cg, self.cpu)
        self.config = {
            "active": True,
            "labelKey": "pool",
            "pools": {"ads": {"cpus": "0,2"}},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_pool_apply_and_disable_resets_to_inherited_mask(self):
        status = self.controller.reconcile(self.config, [self.pod])
        self.assertEqual(status["podCount"], 1)
        self.assertEqual((self.path / "cpuset.cpus").read_text(), "0,2")
        self.controller.reconcile({**self.config, "active": False}, [])
        self.assertEqual((self.path / "cpuset.cpus").read_text(), "\n")
        self.assertEqual(self.controller.managed_cgroups, set())

    def test_label_removal_and_deleted_pod_cleanup(self):
        self.controller.reconcile(self.config, [self.pod])
        self.controller.reconcile(self.config, [])
        self.assertEqual((self.path / "cpuset.cpus").read_text(), "\n")
        self.controller.reconcile(self.config, [self.pod])
        for f in self.path.iterdir():
            f.unlink()
        self.path.rmdir()
        self.controller.reconcile(self.config, [])
        self.assertEqual(self.controller.managed_cgroups, set())

    def test_replacement_pod_gets_same_pool(self):
        self.controller.reconcile(self.config, [self.pod])
        new = json.loads(json.dumps(self.pod))
        new["metadata"]["uid"] = UID[:-1] + "d"
        path = self.cg / module.pod_cgroup(new)
        path.mkdir()
        (path / "cpuset.cpus").write_text("")
        (path / "cpuset.cpus.effective").write_text("0,2")
        self.controller.reconcile(self.config, [new])
        self.assertEqual((self.path / "cpuset.cpus").read_text(), "\n")
        self.assertEqual((path / "cpuset.cpus").read_text(), "0,2")

    def test_replacement_controller_resets_labelled_pod(self):
        self.controller.reconcile(self.config, [self.pod])
        self.controller = module.Controller(self.cg, self.cpu)
        self.controller.reconcile(self.config, [self.pod])
        self.controller.reset()
        self.assertEqual((self.path / "cpuset.cpus").read_text(), "\n")

    def test_multiple_pods_share_the_same_mask(self):
        other = json.loads(json.dumps(self.pod))
        other["metadata"]["uid"] = UID[:-1] + "d"
        path = self.cg / module.pod_cgroup(other)
        path.mkdir()
        (path / "cpuset.cpus").write_text("")
        (path / "cpuset.cpus.effective").write_text("0,2")
        result = self.controller.reconcile(self.config, [self.pod, other])
        self.assertEqual(result["podCount"], 2)
        self.assertEqual(set(result["masks"].values()), {"0,2"})

    def test_replacement_controller_reapplies_cap_and_resets_hardware_bounds(self):
        self.config["pools"]["ads"]["maxFrequencyMHz"] = 2000
        self.controller.reconcile(self.config, [self.pod])
        self.controller = module.Controller(self.cg, self.cpu)
        self.config["pools"]["ads"]["maxFrequencyMHz"] = 2500
        self.controller.reconcile(self.config, [self.pod])
        self.controller.reset()
        self.assertEqual(
            (self.cpu / "cpufreq/policy0/scaling_min_freq").read_text(), "500000"
        )
        self.assertEqual(
            (self.cpu / "cpufreq/policy0/scaling_max_freq").read_text(), "4000000"
        )

    def test_check_interval_uses_seconds(self):
        self.assertEqual(module.check_interval("5s"), 5)
        self.assertEqual(module.check_interval("1.5s"), 1.5)
        for value in ["0s", "61s", "5", "invalid", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.check_interval(value)

    def test_disabled_replacement_resets_remaining_labels_and_configured_policies(self):
        self.config["pools"]["ads"]["maxFrequencyMHz"] = 2000
        self.controller.reconcile(self.config, [self.pod])
        self.controller = module.Controller(self.cg, self.cpu)
        del self.config["pools"]["ads"]["maxFrequencyMHz"]
        self.controller.reconcile({**self.config, "active": False}, [self.pod])
        self.assertEqual((self.path / "cpuset.cpus").read_text(), "\n")
        self.assertEqual(
            (self.cpu / "cpufreq/policy0/scaling_min_freq").read_text(), "500000"
        )
        self.assertEqual(
            (self.cpu / "cpufreq/policy0/scaling_max_freq").read_text(), "4000000"
        )

    def test_restart_only_manages_current_labels_and_pools(self):
        self.config["pools"]["ads"]["maxFrequencyMHz"] = 2000
        self.controller.reconcile(self.config, [self.pod])
        self.controller = module.Controller(self.cg, self.cpu)
        self.controller.reconcile({"active": False, "pools": {}}, [])
        self.assertEqual((self.path / "cpuset.cpus").read_text(), "0,2")
        self.assertEqual(
            (self.cpu / "cpufreq/policy0/scaling_max_freq").read_text(), "2000000"
        )

    def test_validation_precedes_changes(self):
        for mask in ["", "-1", "2-1", "4", "0-99999999"]:
            with self.subTest(mask=mask):
                self.config["pools"]["ads"]["cpus"] = mask
                with self.assertRaises(ValueError):
                    self.controller.reconcile(self.config, [self.pod])
                self.assertEqual((self.path / "cpuset.cpus").read_text(), "\n")
        self.config["pools"] = {"ads": {"cpus": "0"}, "simulation": {"cpus": "2"}}
        with self.assertRaisesRegex(ValueError, "physical core"):
            self.controller.reconcile(self.config, [self.pod])

    def test_unknown_pool_and_missing_cgroup_are_not_silently_skipped(self):
        self.pod["metadata"]["labels"]["pool"] = "typo"
        with self.assertRaisesRegex(ValueError, "unknown CPU pool"):
            self.controller.reconcile(self.config, [self.pod])
        self.pod["metadata"]["labels"]["pool"] = "ads"
        (self.path / "cpuset.cpus.effective").unlink()
        with self.assertRaisesRegex(ValueError, "waiting for cgroup"):
            self.controller.reconcile(self.config, [self.pod])

    def test_parent_constraint_is_validated(self):
        (self.path.parent / "cpuset.cpus.effective").write_text("0-1")
        with self.assertRaisesRegex(ValueError, "parent cgroup"):
            self.controller.reconcile(self.config, [self.pod])

    def test_ineffective_mask_rolls_back(self):
        (self.path / "cpuset.cpus.effective").write_text("0-3")
        with self.assertRaisesRegex(RuntimeError, "not effective"):
            self.controller.reconcile(self.config, [self.pod])
        self.assertEqual((self.path / "cpuset.cpus").read_text(), "\n")

    def test_failed_second_write_rolls_back_first(self):
        self.config["pools"]["ads"]["maxFrequencyMHz"] = 2000
        original = module.write_limits

        def fail(policy, minimum, maximum):
            if policy.name == "policy2" and maximum == 2000000:
                raise OSError("simulated failure")
            original(policy, minimum, maximum)

        with (
            patch.object(module, "write_limits", side_effect=fail),
            self.assertRaisesRegex(OSError, "simulated failure"),
        ):
            self.controller.reconcile(self.config, [self.pod])
        self.assertEqual((self.path / "cpuset.cpus").read_text(), "\n")
        self.assertEqual(
            (self.cpu / "cpufreq/policy0/scaling_max_freq").read_text(), "4000000"
        )
        self.assertEqual(self.controller.managed_frequencies, set())

    def test_frequency_change_and_removal_reset_hardware_bounds(self):
        self.config["pools"]["ads"]["maxFrequencyMHz"] = 2000
        self.controller.reconcile(self.config, [self.pod])
        for number in [0, 2]:
            policy = self.cpu / f"cpufreq/policy{number}"
            self.assertEqual((policy / "scaling_min_freq").read_text(), "500000")
            self.assertEqual((policy / "scaling_max_freq").read_text(), "2000000")
        self.config["pools"]["ads"]["maxFrequencyMHz"] = 3500
        self.controller.reconcile(self.config, [self.pod])
        self.assertEqual(
            (self.cpu / "cpufreq/policy0/scaling_min_freq").read_text(), "500000"
        )
        del self.config["pools"]["ads"]["maxFrequencyMHz"]
        self.controller.reconcile(self.config, [self.pod])
        self.assertEqual(
            (self.cpu / "cpufreq/policy0/scaling_max_freq").read_text(), "4000000"
        )
        self.assertEqual(
            (self.cpu / "cpufreq/policy0/scaling_min_freq").read_text(), "500000"
        )

    def test_cap_requires_siblings_and_entire_policy(self):
        self.config["pools"]["ads"] = {"cpus": "0", "maxFrequencyMHz": 2000}
        with self.assertRaisesRegex(ValueError, "SMT siblings"):
            self.controller.reconcile(self.config, [self.pod])
        self.config["pools"]["ads"]["cpus"] = "0,2"
        (self.cpu / "cpufreq/policy0/related_cpus").write_text("0 1 2")
        with self.assertRaisesRegex(ValueError, "policy crosses"):
            self.controller.reconcile(self.config, [self.pod])

    def test_frequency_range_and_failed_reset_keep_pending_paths(self):
        self.config["pools"]["ads"]["maxFrequencyMHz"] = 9999
        with self.assertRaisesRegex(ValueError, "hardware range"):
            self.controller.reconcile(self.config, [self.pod])
        self.controller.reconcile({**self.config, "active": False}, [])
        self.config["pools"]["ads"]["maxFrequencyMHz"] = 2000
        self.controller.reconcile(self.config, [self.pod])
        with (
            patch.object(module, "write_limits", side_effect=OSError("read-only")),
            self.assertRaisesRegex(RuntimeError, "reset incomplete"),
        ):
            self.controller.reset()
        self.assertEqual(len(self.controller.managed_frequencies), 2)
        self.controller.reset()

    def test_silently_ignored_frequency_reset_keeps_pending_paths(self):
        self.config["pools"]["ads"]["maxFrequencyMHz"] = 2000
        self.controller.reconcile(self.config, [self.pod])
        with (
            patch.object(module, "write_limits"),
            self.assertRaisesRegex(RuntimeError, "reset not effective"),
        ):
            self.controller.reset()
        self.assertEqual(len(self.controller.managed_frequencies), 2)
        self.controller.reset()

    def test_api_selects_only_this_node_namespace_and_label(self):
        with (
            patch.dict(
                os.environ,
                {
                    "KUBERNETES_SERVICE_HOST": "2001:db8::1",
                    "KUBERNETES_SERVICE_PORT": "443",
                },
            ),
            patch.object(Path, "read_text", return_value="test-token\n"),
            patch.object(
                module.ssl, "create_default_context", return_value="tls-context"
            ),
            patch.object(
                module.urllib.request,
                "urlopen",
                return_value=io.StringIO('{"items": []}'),
            ) as request,
        ):
            self.assertEqual(module.list_pods("onboard", "experiment", "pool"), [])
        args, kwargs = request.call_args
        self.assertEqual(kwargs, {"context": "tls-context", "timeout": 10})
        url = urllib.parse.urlsplit(args[0].full_url)
        self.assertEqual(url.netloc, "[2001:db8::1]:443")
        self.assertEqual(url.path, "/api/v1/namespaces/experiment/pods")
        self.assertEqual(
            urllib.parse.parse_qs(url.query),
            {"fieldSelector": ["spec.nodeName=onboard"], "labelSelector": ["pool"]},
        )
        self.assertEqual(args[0].get_header("Authorization"), "Bearer test-token")

    def test_write_limits_never_crosses_current_bounds(self):
        bounds = {"scaling_min_freq": "3000000", "scaling_max_freq": "4000000"}

        def read(path):
            return bounds[path.name]

        def write(path, value):
            bounds[path.name] = value
            self.assertLessEqual(
                int(bounds["scaling_min_freq"]), int(bounds["scaling_max_freq"])
            )

        with (
            patch.object(Path, "read_text", read),
            patch.object(Path, "write_text", write),
        ):
            module.write_limits(Path("policy"), 2000000, 2000000)
            module.write_limits(Path("policy"), 3000000, 4000000)

    def test_all_qos_paths(self):
        for qos, component in [
            ("Guaranteed", "kubepods-pod"),
            ("Burstable", "kubepods-burstable-pod"),
            ("BestEffort", "kubepods-besteffort-pod"),
        ]:
            self.pod["status"]["qosClass"] = qos
            self.assertIn(component, module.pod_cgroup(self.pod))


if __name__ == "__main__":
    unittest.main()
