#!/usr/bin/python3
"""System-bus endpoint. The desktop never supplies paths or shell commands."""

from concurrent.futures import ThreadPoolExecutor
import json
import signal
import subprocess
import threading

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from cpu_power_limits import Error
from cpu_power_limits_view import BUS, PATH, INTERFACE

ENGINE = "/usr/local/libexec/cpu-power-limits"
OPERATIONS = ("save", "test", "end-test", "reapply")


def invoke_engine(operation=None, configuration=None):
    """Transport only: all configuration and hardware decisions belong to the CLI."""
    command = [ENGINE, "--managed", "--json"]
    command += ["--operation", operation] if operation else ["--status"]
    result = subprocess.run(
        command, input=json.dumps(configuration or {}) if operation else None,
        capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
    # A failed application still returns a snapshot describing the actual state.
    if result.returncode not in (0, 1) or not result.stdout.strip():
        raise Error(result.stderr.strip() or "cpu-power-limits did not return a status snapshot")
    try:
        snapshot = json.loads(result.stdout)
    except ValueError as error:
        raise Error("cpu-power-limits returned invalid JSON") from error
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise Error("cpu-power-limits returned an unsupported status snapshot")
    return snapshot


XML = f"""<node><interface name="{INTERFACE}">
<method name="GetStatus"><arg name="snapshot" type="s" direction="out"/></method>
<method name="Apply"><arg name="operation" type="s" direction="in"/>
<arg name="configuration" type="s" direction="in"/>
<arg name="snapshot" type="s" direction="out"/></method>
</interface></node>"""


def authorized(connection, sender, action, interactive):
    subject = ("system-bus-name", {"name": GLib.Variant("s", sender)})
    result = connection.call_sync(
        "org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority",
        "org.freedesktop.PolicyKit1.Authority", "CheckAuthorization",
        GLib.Variant("((sa{sv})sa{ss}us)", (subject, BUS + "." + action, {}, int(interactive), "")),
        GLib.VariantType.new("((bba{ss}))"), Gio.DBusCallFlags.NONE, 120000, None)
    allowed, _, _ = result.unpack()[0]
    if not allowed:
        raise PermissionError("Authorization was cancelled or denied")
    # Do not mutate after a requester disconnects while authenticating.
    connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                         "GetConnectionUnixUser", GLib.Variant("(s)", (sender,)),
                         GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 5000, None)


class Service:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.slots = threading.BoundedSemaphore(8)
        self.registration = 0
        self.connection = None
        self.pending = 0
        self.idle_source = 0

    def acquired(self, connection, _name):
        self.connection = connection
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        self.registration = connection.register_object(PATH, node.interfaces[0], self.method, None, None)
        self.arm_idle()

    def arm_idle(self):
        if self.idle_source:
            GLib.source_remove(self.idle_source)
            self.idle_source = 0
        if not self.pending:
            self.idle_source = GLib.timeout_add_seconds(15, self.expire)
        return GLib.SOURCE_REMOVE

    def expire(self):
        self.idle_source = 0
        if not self.pending:
            self.loop.quit()
        return GLib.SOURCE_REMOVE

    def finished(self):
        self.pending -= 1
        return self.arm_idle()

    def method(self, connection, sender, _path, _interface, method, parameters, invocation):
        if not self.slots.acquire(blocking=False):
            invocation.return_dbus_error(INTERFACE + ".Busy", "Too many pending requests")
            return
        self.pending += 1
        self.arm_idle()

        def run():
            try:
                if method == "GetStatus":
                    authorized(connection, sender, "read", False)
                    snapshot = invoke_engine()
                elif method == "Apply":
                    operation, raw = parameters.unpack()
                    if operation not in OPERATIONS or len(raw.encode()) > 16384:
                        raise Error("Invalid operation or oversized configuration")
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise Error("Invalid configuration object")
                    if operation not in ("test", "save") and data != {}:
                        raise Error("This operation does not accept a configuration")
                    authorized(connection, sender, "change", True)
                    snapshot = invoke_engine(operation, data)
                else:
                    raise Error("Unsupported method")
                invocation.return_value(GLib.Variant("(s)", (json.dumps(snapshot, allow_nan=False),)))
            except PermissionError as error:
                invocation.return_dbus_error(INTERFACE + ".NotAuthorized", str(error))
            except Exception as error:
                invocation.return_dbus_error(INTERFACE + ".Failed", str(error))
            finally:
                self.slots.release()
                GLib.idle_add(self.finished)

        self.executor.submit(run)

    def run(self):
        owner = Gio.bus_own_name(Gio.BusType.SYSTEM, BUS, Gio.BusNameOwnerFlags.NONE,
                                 self.acquired, None, lambda *_: self.loop.quit())
        for signum in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self.loop.quit)
        try:
            self.loop.run()
        finally:
            if self.idle_source:
                GLib.source_remove(self.idle_source)
                self.idle_source = 0
            Gio.bus_unown_name(owner)
            self.executor.shutdown(wait=True)


if __name__ == "__main__":
    Service().run()
