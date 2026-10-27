"""Opt-in integration test on a private session bus, with fake authorization/hardware."""

import json
import os
from pathlib import Path
import sys
import subprocess
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(os.environ.get("CPU_POWER_LIMITS_BUS_TESTS") == "1", "Use dbus-run-session for isolated bus tests")
class HelperBusTests(unittest.TestCase):
    def test_protocol_authorization_denial_and_input_validation(self):
        from gi.repository import Gio, GLib
        import cpu_power_limits_helper as helper
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        service = helper.Service()
        response = {"version": 1, "readings": {"PL1": 80}}
        owner = Gio.bus_own_name_on_connection(connection, helper.BUS, Gio.BusNameOwnerFlags.NONE, None, None)
        service.acquired(connection, helper.BUS)
        # A timer keeps iteration bounded even when a protocol regression loses a reply.
        ticks = [0]
        def tick():
            ticks[0] += 1
            return True
        timer = GLib.timeout_add(20, tick)

        def call(method, parameters=None):
            output = []
            def done(conn, result, _data):
                try:
                    output.append(json.loads(conn.call_finish(result).unpack()[0]))
                except GLib.Error as error:
                    output.append(error)
            connection.call(helper.BUS, helper.PATH, helper.INTERFACE, method, parameters,
                            GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 2000, None, done, None)
            start = ticks[0]
            while not output and ticks[0] - start < 150:
                GLib.MainContext.default().iteration(True)
            self.assertTrue(output, "Missing D-Bus reply")
            return output[0]

        try:
            with patch.object(helper, "authorized") as authorize, patch.object(helper, "invoke_engine", return_value=response) as engine:
                self.assertEqual(call("GetStatus"), response)
                self.assertEqual(authorize.call_args.args[2:], ("read", False))
                self.assertEqual(call("Apply", GLib.Variant("(ss)", ("reapply", "{}"))), response)
                self.assertEqual(authorize.call_args.args[2:], ("change", True))
                engine.assert_called_with("reapply", {})
                engine.reset_mock()
                authorize.side_effect = PermissionError("denied")
                denied = call("Apply", GLib.Variant("(ss)", ("reapply", "{}")))
                self.assertIsInstance(denied, GLib.Error)
                self.assertIn("NotAuthorized", denied.message)
                engine.assert_not_called()
                authorize.reset_mock()
                authorize.side_effect = None
                for operation, payload in (("shell", "{}"), ("revert-test", "{}"), ("reapply", '{"path":"/tmp"}')):
                    invalid = call("Apply", GLib.Variant("(ss)", (operation, payload)))
                    self.assertIsInstance(invalid, GLib.Error)
                authorize.assert_not_called()
                engine.assert_not_called()
        finally:
            GLib.source_remove(timer)
            connection.unregister_object(service.registration)
            Gio.bus_unown_name(owner)
            service.executor.shutdown(wait=True)
            while service.pending:
                GLib.MainContext.default().iteration(True)
            if service.idle_source:
                GLib.source_remove(service.idle_source)

    def test_idle_timeout_waits_for_requests_and_rearms(self):
        from gi.repository import GLib
        import cpu_power_limits_helper as helper
        service = helper.Service()
        service.loop = Mock()
        try:
            service.arm_idle()
            self.assertTrue(service.idle_source)
            service.pending = 1
            service.arm_idle()
            self.assertEqual(service.idle_source, 0)
            service.expire()
            service.loop.quit.assert_not_called()
            service.finished()
            self.assertEqual(service.pending, 0)
            self.assertTrue(service.idle_source)
            GLib.source_remove(service.idle_source)
            service.expire()
            service.loop.quit.assert_called_once()
        finally:
            service.executor.shutdown(wait=True)


@unittest.skipUnless(os.environ.get("CPU_POWER_LIMITS_BUS_TESTS") == "1", "Use dbus-run-session for isolated bus tests")
class IndicatorBusTests(unittest.TestCase):
    def test_resident_exports_indicator_and_menu_without_loading_gtk(self):
        from gi.repository import Gio, GLib
        root = Path(__file__).resolve().parents[1]
        app_id = "io.github.vdmuller.CpuPowerLimits.Indicator.Preview"
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        registrations = []
        xml = '<node><interface name="org.kde.StatusNotifierWatcher"><method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method></interface></node>'
        def register(_conn, _sender, _path, _iface, _method, parameters, invocation):
            registrations.append(parameters.unpack()[0])
            invocation.return_value(None)
        registration = connection.register_object("/StatusNotifierWatcher", Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0], register, None, None)
        owner = Gio.bus_own_name_on_connection(connection, "org.kde.StatusNotifierWatcher", Gio.BusNameOwnerFlags.NONE, None, None)
        process = subprocess.Popen(["/usr/bin/python3", str(root / "cpu_power_limits_gui.py"), "--demo", "--autostart"], stderr=subprocess.PIPE)
        timer = GLib.timeout_add(20, lambda: True)
        def until(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline and process.poll() is None:
                GLib.MainContext.default().iteration(True)
            self.assertTrue(predicate(), "Indicator did not respond")
        def call(path, interface, method, parameters=None):
            result = []
            def done(conn, response, _data):
                try:
                    result.append(conn.call_finish(response).unpack())
                except GLib.Error as error:
                    result.append(error)
            connection.call(app_id, path, interface, method, parameters, None, Gio.DBusCallFlags.NONE, 2000, None, done, None)
            until(lambda: bool(result))
            self.assertNotIsInstance(result[0], GLib.Error)
            return result[0]
        try:
            until(lambda: bool(registrations))
            self.assertEqual(registrations, [app_id])
            # The host may disappear when the GNOME extension is restarted.
            Gio.bus_unown_name(owner)
            until(lambda: not connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner", GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)), None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0])
            owner = Gio.bus_own_name_on_connection(connection, "org.kde.StatusNotifierWatcher", Gio.BusNameOwnerFlags.NONE, None, None)
            until(lambda: len(registrations) == 2)
            maps = Path(f"/proc/{process.pid}/maps").read_text()
            self.assertNotIn("libgtk", maps)
            self.assertNotIn("libgdk-", maps)
            props = call("/StatusNotifierItem", "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", ("org.kde.StatusNotifierItem",)))[0]
            self.assertEqual(props["IconName"], "cpu-power-limits-ok")
            self.assertEqual(props["Menu"], "/Menu")
            self.assertTrue(props["ItemIsMenu"])
            xml = call("/StatusNotifierItem", "org.freedesktop.DBus.Introspectable", "Introspect")[0]
            interface = Gio.DBusNodeInfo.new_for_xml(xml).lookup_interface("org.kde.StatusNotifierItem")
            # GNOME waits for a double click when Activate is advertised, even
            # with ItemIsMenu=true. Menu-only items must omit that method.
            self.assertIsNone(interface.lookup_method("Activate"))
            self.assertIsNotNone(interface.lookup_method("SecondaryActivate"))
            _revision, layout = call("/Menu", "com.canonical.dbusmenu", "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
            entries = {child[1].get("label"): child[0] for child in layout[2]}
            self.assertIn("Open panel", entries)
            self.assertIn("Reapply", entries)
            self.assertIn("100 / 135 W", " ".join(str(key) for key in entries))
            # Exercise the actual menu event path and normal application exit.
            call("/Menu", "com.canonical.dbusmenu", "Event", GLib.Variant("(isvu)", (entries["Quit"], "clicked", GLib.Variant("i", 0), 0)))
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, process.stderr.read().decode())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            process.stderr.close()
            GLib.source_remove(timer)
            connection.unregister_object(registration)
            Gio.bus_unown_name(owner)


if __name__ == "__main__":
    unittest.main()
