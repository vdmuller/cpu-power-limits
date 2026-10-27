"""Regression checks for asynchronous service transitions; no hardware or display."""

from argparse import Namespace
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ServiceTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import cpu_power_limits_gui as gui
        except (ImportError, ValueError, SystemExit) as error:
            raise unittest.SkipTest(str(error))
        cls.gui = gui

    def setUp(self):
        self.app = self.gui.Indicator(Namespace(demo=True))
        self.app.Dbusmenu = Mock()
        self.app.menu = Mock()
        self.app.session = Mock()
        self.app.connection = Mock()
        self.app.connection.call_finish.side_effect = lambda result: result
        self.app.snapshot = self.gui.demo_snapshot("verified")
        self.timer = patch.object(self.gui.GLib, "timeout_add", return_value=42).start()
        self.addCleanup(patch.stopall)

    def query(self):
        self.app.check_service()
        calls = self.app.connection.call.call_args_list[-2:]
        return [call.args[-2] for call in calls]

    def reply(self, callback, properties=None, error=None):
        result = Mock()
        if error:
            self.app.connection.call_finish.side_effect = error
        else:
            result.unpack.return_value = (properties,)
        callback(self.app.connection, result, None)

    def test_job_completion_recovers_icon_without_polling(self):
        _service, unit = self.query()
        self.reply(unit, {"ActiveState": "activating"})
        self.assertEqual(self.app.icon, "cpu-power-limits-applying")
        # Completion is observed even without a final PropertiesChanged signal.
        self.app.service_job_removed(None, None, None, None, None,
            self.gui.GLib.Variant("(uoss)", (7, "/org/freedesktop/systemd1/job/7",
                                           "cpu-power-limits.service", "done")))
        unit = self.app.connection.call.call_args.args[-2]
        self.reply(unit, {"ActiveState": "inactive"})
        self.assertFalse(self.app.service_busy)
        self.assertEqual(self.app.icon, "cpu-power-limits-ok")
        self.timer.assert_not_called()
        self.app.menu.set_root.assert_called()
        self.app.session.emit_signal.assert_any_call(
            None, self.gui.SNI_PATH, self.gui.SNI, "NewIcon", None)

    def test_late_reply_cannot_restore_busy_or_old_failure(self):
        old_service, old_unit = self.query()
        service, unit = self.query()
        self.reply(unit, {"ActiveState": "inactive"})
        self.reply(service, {"Result": "success"})
        self.reply(old_unit, {"ActiveState": "activating"})
        self.reply(old_service, {"Result": "exit-code", "ExecMainExitTimestamp": 10**18})
        self.assertFalse(self.app.service_busy)
        self.assertEqual(self.app.service_error, "")
        self.assertEqual(self.app.icon, "cpu-power-limits-ok")
        self.timer.assert_not_called()

    def test_busy_transitions_never_schedule_polling(self):
        for state in ("activating", "deactivating", "reloading", "inactive"):
            _service, unit = self.query()
            self.reply(unit, {"ActiveState": state})
        self.timer.assert_not_called()

    def test_unrelated_job_does_not_trigger_queries(self):
        self.app.service_job_removed(None, None, None, None, None,
            self.gui.GLib.Variant("(uoss)", (7, "/org/freedesktop/systemd1/job/7",
                                           "unrelated.service", "done")))
        self.app.connection.call.assert_not_called()

    def test_query_failure_clears_busy(self):
        _service, unit = self.query()
        self.reply(unit, {"ActiveState": "activating"})
        _service, unit = self.query()
        self.reply(unit, error=self.gui.GLib.Error("Unit unavailable"))
        self.assertFalse(self.app.service_busy)
        self.timer.assert_not_called()

    def test_shutdown_ignores_pending_reply(self):
        _service, unit = self.query()
        self.reply(unit, {"ActiveState": "activating"})
        _service, pending = self.query()
        self.app.close_status()
        self.app.session.emit_signal.reset_mock()
        self.reply(pending, {"ActiveState": "activating"})
        self.app.session.emit_signal.assert_not_called()
        self.timer.assert_not_called()

    def test_icon_properties_include_final_values_and_skip_duplicate_signals(self):
        self.app.service_busy = True
        self.app.render()
        self.app.session.emit_signal.reset_mock()
        self.app.service_busy = False
        self.app.render()
        calls = self.app.session.emit_signal.call_args_list
        properties = [call.args[4].unpack() for call in calls
                      if call.args[3] == "PropertiesChanged"]
        self.assertEqual(len(properties), 1)
        interface, changed, invalidated = properties[0]
        self.assertEqual(interface, self.gui.SNI)
        self.assertEqual(changed["IconName"], "cpu-power-limits-ok")
        self.assertEqual(changed["ToolTip"][0], "cpu-power-limits-ok")
        self.assertEqual(invalidated, [])
        self.app.session.emit_signal.reset_mock()
        self.app.render()
        self.app.session.emit_signal.assert_not_called()
        self.timer.assert_not_called()

    def test_open_panel_waits_asynchronously_and_coalesces_clicks(self):
        with patch.object(self.gui, "send_action_async") as send, \
             patch.object(self.gui.Gio.Subprocess, "new") as spawn:
            self.app.open_panel()
            self.app.open_panel()
            send.assert_called_once()
            spawn.assert_not_called()
            send.call_args.args[2](True)
            self.assertFalse(self.app.open_pending)
            spawn.assert_not_called()

    def test_open_panel_starts_one_child_when_panel_is_absent(self):
        self.app.options.demo_state = "verified"
        with patch.object(self.gui, "send_action_async") as send, \
             patch.object(self.gui.Gio.Subprocess, "new") as spawn:
            self.app.open_panel()
            send.call_args.args[2](False)
            spawn.assert_called_once()
            child = spawn.return_value
            child.wait_async.assert_called_once()
            self.app.open_panel()
            send.call_args.args[2](False)
            spawn.assert_called_once()

    def test_async_action_reports_result_without_synchronous_bus_calls(self):
        connection, callback = Mock(), Mock()
        with patch.object(self.gui.Gio, "bus_get") as get, \
             patch.object(self.gui.Gio, "bus_get_finish", return_value=connection), \
             patch.object(self.gui.Gio, "bus_get_sync") as sync:
            self.gui.send_action_async("example.Panel", "open-status", callback)
            callback.assert_not_called()
            get.call_args.args[2](None, None, None)
            callback.assert_not_called()
            completed = connection.call.call_args.args[-2]
            completed(connection, None, None)
            callback.assert_called_once_with(True)
            callback.reset_mock()
            connection.call_finish.side_effect = self.gui.GLib.Error("No owner")
            completed(connection, None, None)
            callback.assert_called_once_with(False)
            connection.call_sync.assert_not_called()
            sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
