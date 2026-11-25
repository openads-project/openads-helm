from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))

from controller import DEFAULT, TrafficControl, validate


def config():
    return {
        "ifb": "ifb-test", "priority": 49000, "peer": "192.0.2.1",
        "flows": [{"destinationPort": 7448}, {"sourcePort": 8554}],
        "owner": "test/controller",
    }


class TestController(unittest.TestCase):
    def test_peer_route_selects_interface_and_address(self):
        ctrl = TrafficControl(config())
        with patch("controller.run", return_value='[{"dev":"wan0","prefsrc":"192.0.2.2"}]') as command:
            self.assertEqual(ctrl.peer_route(), ("wan0", "192.0.2.2"))
            command.assert_called_once_with("ip", "-4", "-j", "route", "get", "192.0.2.1")
        for route in ("[]", '[{"type":"local","dev":"lo","prefsrc":"192.0.2.1"}]', '[{"dev":"wan0"}]'):
            with self.subTest(route=route), patch("controller.run", return_value=route):
                with self.assertRaisesRegex(RuntimeError, "route to 192.0.2.1"):
                    ctrl.peer_route()

    def test_configured_defaults_seed_profile_in_bypass(self):
        netem = {
            "delay": "25ms", "jitter": "4ms", "loss": "2%", "rate": "10mbit",
            "distribution": "pareto", "limit": 300,
        }
        ctrl = TrafficControl(config() | {"netem": netem})
        self.assertEqual(ctrl.profile, netem | {"bypass": True})
        self.assertEqual(validate({"bypass": False}, ctrl.profile), netem | {"bypass": False})
        with self.assertRaisesRegex(ValueError, "invalid delay"):
            TrafficControl(config() | {"netem": {"delay": "invalid"}})

    def test_rejects_topology_edits_and_malformed_profiles(self):
        for payload in (
            {"interface": "eth0"}, {"loss": "101%"}, {"rate": "0mbit"},
            {"bypass": "false"}, {"dryRun": "false"}, {"limit": False},
            {"distribution": []}, {"jitter": None},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate(payload, DEFAULT)

    def test_idempotent_profile_and_bypass(self):
        ctrl = TrafficControl(config())
        ctrl.bypass = Mock()
        ctrl.install_redirects = Mock()
        ctrl.set_queue = Mock()
        profile = validate({"bypass": False, "delay": "20ms"}, DEFAULT)
        ctrl.apply(profile)
        ctrl.apply(profile)
        ctrl.install_redirects.assert_called_once_with()
        ctrl.set_queue.assert_called_once_with(profile, "change")
        ctrl.apply(DEFAULT)
        self.assertEqual(ctrl.profile, DEFAULT)
        self.assertEqual(ctrl.bypass.call_count, 2)

    def test_rolls_back_failed_update(self):
        ctrl = TrafficControl(config())
        previous = validate({"delay": "20ms", "bypass": False}, DEFAULT)
        ctrl.profile = previous
        ctrl.set_queue = Mock(side_effect=[RuntimeError("injected failure"), None])
        with self.assertRaisesRegex(RuntimeError, "injected failure"):
            ctrl.apply(previous | {"delay": "40ms"})
        ctrl.set_queue.assert_called_with(previous, "change")
        self.assertEqual(ctrl.profile, previous)
        self.assertFalse(ctrl.degraded)

    def test_failed_restore_removes_owned_redirects(self):
        ctrl = TrafficControl(config())
        ctrl.interface = "wan0"
        ctrl.profile = validate({"delay": "20ms", "bypass": False}, DEFAULT)
        ctrl.set_queue = Mock(side_effect=RuntimeError("tc failed"))
        ctrl.remove_redirects = Mock()
        with self.assertRaisesRegex(RuntimeError, "restore failed"):
            ctrl.apply(ctrl.profile | {"delay": "40ms"})
        ctrl.remove_redirects.assert_called_once_with("wan0")
        self.assertTrue(ctrl.degraded)
        self.assertTrue(ctrl.profile["bypass"])

    def test_foreign_filters_are_rejected(self):
        ctrl = TrafficControl(config())
        filter_info = {"pref": ctrl.priority, "protocol": "ip", "kind": "flower"}
        ctrl.filters = Mock(return_value=[filter_info | {"options": {"actions": [{"cookie": ctrl.cookie}]}}])
        self.assertEqual(ctrl.owned_priorities("wan0"), [ctrl.priority])
        ctrl.filters.return_value.append(filter_info | {"options": {"actions": [{"cookie": "foreign"}]}})
        with self.assertRaisesRegex(RuntimeError, "belongs to another"):
            ctrl.owned_priorities("wan0")
