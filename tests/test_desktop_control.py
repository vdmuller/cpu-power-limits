"""Desktop state and boot-local transactions, using synthetic hardware only."""

import json
import os
import gettext
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_cpu_power_limits import Fixture, FakeMailbox, CONFIG, REPO
import cpu_power_limits as core
from cpu_power_limits_control import Controller, configuration_wire, validate_configuration
import cpu_power_limits_view as view


class ControlTests(Fixture):
    def setUp(self):
        super().setUp()
        self.control = Controller(config=self.config, runtime=self.root / "runtime", powercap=str(self.zone.parent),
                                  supply=str(self.supply.parent), msr=str(self.root / "msr"),
                                  lock=str(self.root / "control.lock"))
        self.mailbox = FakeMailbox({0: core.encode_offset("20"), 1: core.encode_offset("30")})
        self.addCleanup(patch.stopall)
        patch.object(core, "Mailbox", return_value=self.mailbox).start()
        self.data = configuration_wire(core.read_config(self.config))

    def change(self, operation, **updates):
        return self.control.change(operation, dict(self.data, **updates) if operation in ("test", "save") else None)

    def test_refresh_never_mutates_limits_or_offsets(self):
        before = self.snapshot()
        status = self.control.refresh()
        self.assertEqual(status["profile"], "ac")
        self.assertEqual(status["readings"]["PL1"], 45)
        self.assertEqual(status["expected"]["PL1"], 80)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mailbox.writes, [])
        self.assertIsNone(status["last_application"])
        self.assertEqual(self.control.state.stat().st_mode & 0o777, 0o644)

    def test_real_managed_cli_accepts_unmanaged_unavailable_msr_and_reports_failures(self):
        env = dict(os.environ, **self.env, CPU_POWER_LIMITS_RUNTIME_ROOT=str(self.root / "cli-runtime"))
        command = [sys.executable, str(REPO / "cpu_power_limits.py"), "--managed", "--config", str(self.config)]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = subprocess.run([*command, "--status"], env=env, capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.config.write_text("invalid")
        failure = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(failure.returncode, 1)
        self.assertNotIn("Traceback", failure.stderr)
        saved = json.loads((self.root / "cli-runtime/status.json").read_text())
        self.assertFalse(saved["last_application"]["success"])

    def test_cli_operations_json_and_persistence(self):
        env = dict(os.environ, **self.env, CPU_POWER_LIMITS_RUNTIME_ROOT=str(self.root / "cli-runtime"))
        command = [sys.executable, str(REPO / "cpu_power_limits.py"), "--managed", "--json", "--config", str(self.config)]
        data = dict(self.data, AC_PL1="70")
        original = self.config.read_bytes()
        for operation, payload, source, expected in (
                ("test", data, "temporary", 70),
                ("reapply", {}, "temporary", 70),
                ("end-test", {}, "saved", 80),
                ("save", data, "saved", 70)):
            with self.subTest(operation=operation):
                result = subprocess.run([*command, "--operation", operation], input=json.dumps(payload),
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                snapshot = json.loads(result.stdout)
                self.assertTrue(snapshot["last_application"]["success"])
                self.assertEqual(snapshot["source"], source)
                self.assertEqual(snapshot["readings"]["PL1"], expected)
                if operation != "save":
                    self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(core.read_config(self.config)["AC_PL1"], 70000000)
        status = subprocess.run([*command, "--status"], env=env, capture_output=True, text=True)
        self.assertEqual(json.loads(status.stdout)["readings"]["PL1"], 70)

    def test_cli_rejects_invalid_operation_payloads_without_writes(self):
        env = dict(os.environ, **self.env, CPU_POWER_LIMITS_RUNTIME_ROOT=str(self.root / "cli-runtime"))
        command = [sys.executable, str(REPO / "cpu_power_limits.py"), "--managed", "--json"]
        before = self.snapshot()
        original = self.config.read_bytes()
        for operation, payload in (("save", '{"AC_PL1":"$(id)"}'), ("save", "[]"),
                                   ("save", "{"), ("save", " " * 16385),
                                   ("reapply", '{"path":"/tmp"}')):
            with self.subTest(operation=operation, size=len(payload)):
                result = subprocess.run([*command, "--config", str(self.config), "--operation", operation],
                                        input=payload, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.config.read_bytes(), original)

    def test_test_survives_new_process_power_changes_and_reapply(self):
        original = self.config.read_bytes()
        result = self.change("test", AC_PL1="70", BATTERY_PL1="25", CORE_OFFSET="150")
        self.assertTrue(result["last_application"]["success"])
        self.assertEqual(result["source"], "temporary")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.limits.read()[0], 70000000)
        (self.supply / "online").write_text("0")
        fresh = Controller(config=self.config, runtime=self.control.runtime, powercap=self.control.powercap,
                           supply=self.control.supply, msr=self.control.msr, lock=self.control.lock)
        status = fresh.change("reapply")
        self.assertTrue(status["last_application"]["success"])
        self.assertEqual(self.limits.read()[0], 25000000)
        self.assertEqual(self.mailbox.values[0], core.encode_offset("150"))

    def test_end_test_accepts_state_left_by_removed_revert_operation(self):
        original = self.config.read_bytes()
        for mode, reverted in (("temporary", True), ("reverting", False)):
            with self.subTest(mode=mode):
                self.change("test", AC_PL1="70", CORE_OFFSET="150")
                legacy = self.control.read_override()
                legacy.update(mode=mode, reverted=reverted)
                legacy["configuration"]["AC_PL1"] = "45.5"
                self.control.write_override(legacy)
                result = self.change("end-test")
                self.assertTrue(result["last_application"]["success"])
                self.assertEqual(result["source"], "saved")
                self.assertEqual(self.limits.read(), (80000000, 110000000))
                self.assertEqual(self.mailbox.values[0], core.encode_offset("20"))
                self.assertEqual(self.config.read_bytes(), original)
                self.assertFalse(self.control.override.exists())

    def test_end_restores_original_unmanaged_offsets(self):
        baseline = dict(self.mailbox.values)
        self.change("test", CORE_OFFSET="150", IGPU_OFFSET="80")
        self.change("test", CORE_OFFSET="200", IGPU_OFFSET="90")
        result = self.change("end-test")
        self.assertTrue(result["last_application"]["success"])
        self.assertEqual(result["source"], "saved")
        self.assertFalse(self.control.override.exists())
        self.assertEqual(self.mailbox.values, baseline)
        self.assertEqual(self.limits.read(), (80000000, 110000000))

    def test_deselecting_an_offset_during_test_restores_its_original_value(self):
        self.change("test", CORE_OFFSET="150")
        result = self.change("test", AC_PL1="70")
        self.assertTrue(result["last_application"]["success"])
        self.assertEqual(self.mailbox.values[0], core.encode_offset("20"))

    def test_save_from_test_keeps_managed_domains_and_restores_others(self):
        self.config.write_text("# Keep this note\n" + CONFIG + "# Extra comment\n")
        self.change("test", CORE_OFFSET="150", IGPU_OFFSET="80")
        result = self.change("save", AC_PL1="90", CORE_OFFSET="100")
        self.assertTrue(result["last_application"]["success"])
        self.assertTrue(result["last_application"]["configuration_saved"])
        self.assertEqual(core.read_config(self.config)["AC_PL1"], 90000000)
        self.assertIn("# Keep this note", self.config.read_text())
        self.assertIn("# Extra comment", self.config.read_text())
        self.assertEqual(self.mailbox.values[0], core.encode_offset("100"))
        self.assertEqual(self.mailbox.values[1], core.encode_offset("30"))
        self.assertFalse(self.control.override.exists())

    def test_failed_application_is_published_with_partial_readings(self):
        self.mailbox.fail = 0
        result = self.change("test", CORE_OFFSET="150")
        last = result["last_application"]
        self.assertFalse(last["success"])
        self.assertIn("injected", last["error"])
        self.assertEqual(result["readings"]["PL1"], 80)
        self.assertEqual(result["readings"]["CORE_OFFSET"], float(core.decode_offset(core.encode_offset("150"))))
        self.assertTrue(self.control.override.exists())
        self.assertEqual(json.loads(self.control.state.read_text())["last_application"]["id"], last["id"])

    def test_failed_restore_retains_baseline_for_retry(self):
        self.change("test", CORE_OFFSET="150")
        self.mailbox.fail = 0
        result = self.change("end-test")
        self.assertFalse(result["last_application"]["success"])
        self.assertTrue(result["restoration_pending"])
        result = self.change("reapply")
        self.assertTrue(result["last_application"]["success"])
        self.assertFalse(self.control.override.exists())

    def test_save_and_apply_failure_reports_saved_separately(self):
        self.mailbox.read_fail.add(0)
        result = self.change("save", AC_PL1="90", CORE_OFFSET="100")
        self.assertFalse(result["last_application"]["success"])
        self.assertTrue(result["last_application"]["configuration_saved"])
        self.assertEqual(core.read_config(self.config)["AC_PL1"], 90000000)

    def test_unreadable_and_positive_baselines_refuse_test_before_changes(self):
        before = self.snapshot()
        self.mailbox.read_fail.add(0)
        result = self.change("test", CORE_OFFSET="150")
        self.assertFalse(result["last_application"]["success"])
        self.assertFalse(self.control.override.exists())
        self.assertEqual(self.snapshot(), before)
        self.mailbox.read_fail.clear()
        self.mailbox.values[0] = 1
        result = self.change("test", CORE_OFFSET="150")
        self.assertFalse(result["last_application"]["success"])
        self.assertIn("positive", result["last_application"]["error"])
        self.assertEqual(self.mailbox.writes, [])

    def test_missing_runtime_after_boot_selects_saved_configuration(self):
        self.change("test", AC_PL1="70")
        self.control.override.unlink()  # /run is recreated at boot.
        result = self.change("reapply")
        self.assertEqual(result["source"], "saved")
        self.assertEqual(self.limits.read()[0], 80000000)

    def test_invalid_saved_and_temporary_configuration_never_claim_success(self):
        self.config.write_text("invalid")
        result = self.change("reapply")
        self.assertFalse(result["last_application"]["success"])
        self.assertTrue(result["errors"])
        self.control.override.write_text("[]")
        result = self.change("reapply")
        self.assertFalse(result["last_application"]["success"])
        self.assertTrue(any(error["code"] == "temporary" for error in result["errors"]))

    def test_invalid_requests_and_world_writable_runtime_are_rejected(self):
        before = self.snapshot()
        for updates in ({"AC_PL1": "0"}, {"AC_PL1": "200"}, {"CORE_OFFSET": "1001"},
                        {"UNKNOWN": "1"}, {"CORE_OFFSET": "$(id)"}, {"AC_PL1": 70}):
            with self.subTest(updates=updates), self.assertRaises(core.Error):
                self.control.change("test", dict(self.data, **updates))
        with self.assertRaises(core.Error):
            self.control.change("shell")
        self.control.runtime.mkdir(mode=0o777)
        self.control.runtime.chmod(0o777)
        with self.assertRaises(core.Error):
            self.control.refresh()
        self.assertEqual(self.snapshot(), before)


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"configuration": {"AC_PL1": "100"}, "profile": "ac", "readings": {"PL1": 100, "UNCORE_OFFSET": 0},
                         "expected": {"PL1": 100}, "last_application": None}

    def test_card_semantics_do_not_depend_on_colors(self):
        self.assertEqual(view.card(self.snapshot, "PL1")["state"], "verified")
        self.assertFalse(view.card(self.snapshot, "PL1")["show_expected"])
        self.snapshot["readings"]["PL1"] = 80
        self.assertEqual(view.card(self.snapshot, "PL1")["state"], "warning")
        self.assertTrue(view.card(self.snapshot, "PL1")["show_expected"])
        self.assertEqual(view.card(self.snapshot, "UNCORE_OFFSET")["state"], "unmanaged")
        self.snapshot["readings"]["PL1"] = None
        self.assertEqual(view.card(self.snapshot, "PL1")["state"], "unavailable")

    def test_overall_status_handles_missing_records_and_failures(self):
        self.assertEqual(view.overall(self.snapshot)[0], "verified")
        self.assertEqual(view.overall(self.snapshot, "service timeout")[0], "error")
        self.snapshot["last_application"] = {"id": "1", "error": "firmware rejected", "success": False}
        self.assertEqual(view.overall(self.snapshot)[0], "error")
        self.assertTrue(view.new_failure(self.snapshot, "0"))
        self.assertFalse(view.new_failure(self.snapshot, "1"))


class DesktopPackagingTests(unittest.TestCase):
    def test_translation_catalog_and_desktop_entry_are_installed_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            shutil.copytree(REPO, source, ignore=shutil.ignore_patterns(".git", "__pycache__", "design-preview"))
            (source / "po/LINGUAS").write_text("zz\n")
            (source / "po/zz.po").write_text('''msgid ""
msgstr ""
"Project-Id-Version: cpu-power-limits 1\\n"
"PO-Revision-Date: 2026-10-26 00:00+0000\\n"
"Last-Translator: Test\\n"
"Language-Team: Test\\n"
"Language: zz\\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"

msgid "Status"
msgstr "Translated status"

msgid "Inspect and configure CPU power limits and undervolting"
msgstr "Translated application description"
''')
            stage = root / "stage"
            stage.mkdir()
            result = subprocess.run([str(source / "install.sh"), "--root", str(stage)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            catalog = gettext.translation("cpu-power-limits", localedir=stage / "usr/local/share/locale", languages=["zz"])
            self.assertEqual(catalog.gettext("Status"), "Translated status")
            self.assertEqual(catalog.gettext("Missing translation"), "Missing translation")
            desktop = (stage / "usr/local/share/applications/io.github.vdmuller.CpuPowerLimits.Indicator.desktop").read_text()
            self.assertIn("Comment[zz]=Translated application description", desktop)
            removed = subprocess.run([str(source / "uninstall.sh"), "--root", str(stage)], capture_output=True, text=True)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse((stage / "usr/local/share/locale/zz/LC_MESSAGES/cpu-power-limits.mo").exists())

    def test_default_install_and_remove_include_desktop_and_keep_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed = subprocess.run([str(REPO / "install.sh"), "--root", tmp], capture_output=True, text=True)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            names = ["usr/local/bin/cpu-power-limits-indicator", "usr/local/libexec/cpu-power-limits-helper",
                     "usr/local/libexec/cpu_power_limits.py", "usr/local/libexec/cpu_power_limits_control.py",
                     "usr/local/libexec/cpu_power_limits_helper.py", "usr/local/libexec/cpu_power_limits_view.py",
                     "usr/local/share/applications/io.github.vdmuller.CpuPowerLimits.Indicator.desktop", "etc/xdg/autostart/cpu-power-limits.desktop",
                     "etc/systemd/system/cpu-power-limits-helper.service",
                     "etc/dbus-1/system.d/io.github.vdmuller.CpuPowerLimits.conf",
                     "usr/share/polkit-1/actions/io.github.vdmuller.CpuPowerLimits.policy",
                     "usr/share/dbus-1/system-services/io.github.vdmuller.CpuPowerLimits.service",
                     "usr/local/libexec/icons/cpu-power-limits-ok.svg"]
            for name in names:
                self.assertTrue((root / name).exists(), name)
            unrelated = root / "usr/local/libexec/icons/unrelated.svg"
            unrelated.write_text("keep")
            for _ in range(2):
                removed = subprocess.run([str(REPO / "uninstall.sh"), "--root", tmp], capture_output=True, text=True)
                self.assertEqual(removed.returncode, 0, removed.stderr)
            for name in names:
                self.assertFalse((root / name).exists(), name)
            self.assertEqual(unrelated.read_text(), "keep")
            self.assertTrue((root / "etc/cpu-power-limits.conf").exists())

    def test_helper_delegates_to_fixed_cli_and_strips_environment_overrides(self):
        import cpu_power_limits_helper as helper
        from unittest.mock import Mock
        response = {"version": 1, "last_application": {"success": False}}
        with patch.object(helper.subprocess, "run", return_value=Mock(
                returncode=1, stdout=json.dumps(response), stderr="application failed")) as run:
            self.assertEqual(helper.invoke_engine("save", {"AC_PL1": "70"}), response)
            args, kwargs = run.call_args
            self.assertEqual(args[0], [helper.ENGINE, "--managed", "--json", "--operation", "save"])
            self.assertEqual(json.loads(kwargs["input"]), {"AC_PL1": "70"})
            self.assertNotIn("shell", kwargs)
            self.assertFalse(any(key.startswith("CPU_POWER_LIMITS_") for key in kwargs["env"]))
            helper.invoke_engine()
            self.assertEqual(run.call_args.args[0], [helper.ENGINE, "--managed", "--json", "--status"])
        for output in ("", "bad JSON", "[]", '{"version":2}'):
            with patch.object(helper.subprocess, "run", return_value=Mock(returncode=1, stdout=output, stderr="failed")):
                with self.assertRaises(core.Error):
                    helper.invoke_engine()

    def test_helper_uses_only_sender_identity_and_authorization_result(self):
        try:
            from cpu_power_limits_helper import authorized, BUS, INTERFACE, XML
            from gi.repository import GLib, Gio
        except ImportError:
            self.skipTest("PyGObject not installed")
        from unittest.mock import Mock
        connection = Mock()
        connection.call_sync.side_effect = [GLib.Variant("((bba{ss}))", ((True, False, {}),)), GLib.Variant("(u)", (1000,))]
        authorized(connection, ":1.42", "change", True)
        payload = connection.call_sync.call_args_list[0].args[4].unpack()
        self.assertEqual(payload[0], ("system-bus-name", {"name": ":1.42"}))
        self.assertEqual(payload[1], BUS + ".change")
        self.assertEqual(payload[3], 1)
        connection.call_sync.return_value = GLib.Variant("((bba{ss}))", ((False, True, {}),))
        connection.call_sync.side_effect = None
        with self.assertRaises(PermissionError):
            authorized(connection, ":1.42", "change", True)
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        self.assertEqual(node.interfaces[0].name, INTERFACE)
        self.assertEqual({method.name for method in node.interfaces[0].methods}, {"GetStatus", "Apply"})


if __name__ == "__main__":
    unittest.main()
