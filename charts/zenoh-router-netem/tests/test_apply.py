import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "netem_apply", Path(__file__).resolve().parents[1] / "files/netem/apply.py"
)
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)


class FilterUpdateTest(unittest.TestCase):
    def exercise(self, old_priorities, fail_add=None, **selectors):
        calls = []
        additions = 0

        def run(cmd, check=True):
            nonlocal additions
            calls.append(cmd)
            if "show" in cmd:
                return SimpleNamespace(stdout=json.dumps([
                    {"kind": "u32", "pref": pref} for pref in old_priorities
                ]))
            if "add" in cmd:
                additions += 1
                if additions == fail_add:
                    raise RuntimeError("injected installation failure")
            return SimpleNamespace(stdout="")

        with patch.object(apply, "run", side_effect=run):
            try:
                apply.update_filters("eth0", **selectors)
            except RuntimeError:
                if fail_add is None:
                    raise
        return calls

    def test_all_new_matches_exist_before_any_old_match_is_removed(self):
        calls = self.exercise([1, 2], target_ips=["192.0.2.1", "192.0.2.2"], peer_port="30448")
        mutations = calls[1:]
        self.assertEqual([cmd[2] for cmd in mutations], ["add", "add", "del", "del"])
        self.assertTrue(all(cmd[cmd.index("prio") + 1] == "3" for cmd in mutations[:2]))
        self.assertIn("192.0.2.1/32", mutations[0])
        self.assertIn("192.0.2.2/32", mutations[1])
        self.assertIn("30448", mutations[0])

    def test_failed_install_keeps_every_old_filter(self):
        calls = self.exercise([1], fail_add=2, target_ips=["192.0.2.1", "192.0.2.2"], peer_port="")
        self.assertFalse(any("del" in cmd for cmd in calls))

    def test_port_only_and_initial_install(self):
        calls = self.exercise([], target_ips=[], peer_port="30448")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("dst", calls[-1])
        self.assertIn("dport", calls[-1])

    def test_unexpected_filters_are_preserved(self):
        with patch.object(apply, "run", return_value=SimpleNamespace(
            stdout='[{"kind":"flower","pref":1}]'
        )) as run, self.assertRaisesRegex(RuntimeError, "unexpected filter"):
            apply.update_filters("eth0", ["192.0.2.1"], "")
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
