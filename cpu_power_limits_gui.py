#!/usr/bin/python3
"""A small resident status indicator with a separate, on-demand GTK panel."""

import argparse
import gettext
import json
import locale
from pathlib import Path
import sys
import time

import gi
from gi.repository import Gio, GLib

import cpu_power_limits_view as view
from cpu_power_limits_view import BUS, PATH, INTERFACE

APP_ID = BUS + ".Indicator"
DOMAIN = "cpu-power-limits"
gettext.bindtextdomain(DOMAIN, "/usr/local/share/locale")
gettext.textdomain(DOMAIN)
_ = gettext.gettext
view._ = _
view.LABELS.update(CORE_OFFSET=_("Core"), CACHE_OFFSET=_("Cache"), IGPU_OFFSET=_("iGPU"), UNCORE_OFFSET=_("Uncore"))
RUNTIME = Path("/run/cpu-power-limits")
SERVICE_PATH = "/org/freedesktop/systemd1/unit/cpu_2dpower_2dlimits_2eservice"


def indicator_id(options):
    return APP_ID + (".Preview" if options.demo else "")


def panel_id(options):
    return APP_ID + ".Panel" + (".Preview" if options.demo else "")


def action_path(app_id):
    return "/" + app_id.replace(".", "/")


def send_action(app_id, action, wait=True):
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        arguments = (app_id, action_path(app_id), "org.gtk.Actions", "Activate",
                     GLib.Variant("(sava{sv})", (action, [], {})), None,
                     Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
        if wait:
            connection.call_sync(*arguments)
        else:
            connection.call(*arguments, None, None)
            connection.flush_sync(None)
        return True
    except GLib.Error:
        return False


class StatusClient:
    """Share event-driven state handling without importing a graphical toolkit."""

    def init_status(self, options, panel=False):
        self.options = options
        self.panel_mode = panel
        self.snapshot = {}
        self.connection = None
        self.busy = False
        self.service_busy = False
        self.dirty = False
        self.loading = False
        self.seen_application = None
        self.initial_snapshot = True
        self.first_activation = True
        self.service_error = ""
        self.query_error = ""
        self.monitors = []
        self.signal_ids = []

    def close_status(self):
        for monitor in self.monitors:
            monitor.cancel()
        if self.connection:
            for signal_id in self.signal_ids:
                self.connection.signal_unsubscribe(signal_id)

    def bus_ready(self, _source, result, _data):
        try:
            self.connection = Gio.bus_get_finish(result)
            self.signal_ids.append(self.connection.signal_subscribe("org.freedesktop.systemd1", "org.freedesktop.DBus.Properties", "PropertiesChanged", SERVICE_PATH, None, Gio.DBusSignalFlags.NONE, lambda *_: self.check_service()))
            self.connection.call("org.freedesktop.systemd1", "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "Subscribe", None, None, Gio.DBusCallFlags.NONE, 5000, None, None, None)
            self.check_service()
            if self.panel_mode or not self.snapshot:
                self.request()
        except GLib.Error as error:
            self.show_notice(_("Could not connect to the system service. %(detail)s") % {"detail": error.message})

    def check_service(self):
        if not self.connection:
            return
        def done(connection, result, _data):
            try:
                properties = connection.call_finish(result).unpack()[0]
                result_value = properties.get("Result", "success")
                exited = properties.get("ExecMainExitTimestamp", 0) / 1000000
                last = self.snapshot.get("last_application") or {}
                try:
                    from datetime import datetime
                    applied = datetime.fromisoformat(last.get("timestamp", "")).timestamp()
                except ValueError:
                    applied = 0
                self.service_error = _("The automatic application service failed (%(result)s).") % {"result": result_value} if result_value != "success" and exited > applied else ""
                self.render()
            except GLib.Error:
                pass
        self.connection.call("org.freedesktop.systemd1", SERVICE_PATH, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", ("org.freedesktop.systemd1.Service",)), None, Gio.DBusCallFlags.NONE, 5000, None, done, None)
        def unit_done(connection, result, _data):
            try:
                properties = connection.call_finish(result).unpack()[0]
                self.service_busy = properties.get("ActiveState") in ("activating", "deactivating", "reloading")
            except GLib.Error:
                self.service_busy = False
            self.render()
        self.connection.call("org.freedesktop.systemd1", SERVICE_PATH, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", ("org.freedesktop.systemd1.Unit",)), None, Gio.DBusCallFlags.NONE, 5000, None, unit_done, None)

    def request(self, operation=None, data=None):
        if self.busy or self.service_busy:
            return
        if self.options.demo:
            self.show_notice(_("Preview only. No hardware was accessed."))
            return
        if not self.connection:
            self.show_notice(_("The system service is not connected. Reopen the application to retry."))
            return
        self.busy = True
        self.render()
        method = "Apply" if operation else "GetStatus"
        parameters = GLib.Variant("(ss)", (operation, json.dumps(data or {}))) if operation else None
        def done(connection, result, _data):
            self.busy = False
            try:
                response = json.loads(connection.call_finish(result).unpack()[0])
                if not view.valid_snapshot(response):
                    raise ValueError(_("The service returned an unsupported status format."))
                if operation:
                    last = response.get("last_application") or {}
                    effective = response.get("configuration", {})
                    accepted_test = operation == "test" and response.get("source") == "temporary" and data.keys() == effective.keys() and all(float(value) == float(effective[key]) for key, value in data.items())
                    if last.get("success") or last.get("configuration_saved") or accepted_test:
                        self.dirty = False
                self.receive(response)
                if operation and not self.dirty:
                    self.load_editor()
                self.watch_runtime()
                self.check_service()
                if operation and self.panel_mode:
                    last = response.get("last_application") or {}
                    text = _("Configuration saved; application failed.") if last.get("configuration_saved") and not last.get("success") else _("Application failed. See Status for details.") if not last.get("success") else _("Configuration saved and applied.") if operation == "save" else _("Temporary configuration applied for this boot.") if operation == "test" else _("Saved configuration restored.") if operation == "end-test" else _("Configuration applied and verified.")
                    self.show_notice(text)
            except (GLib.Error, ValueError) as error:
                detail = str(error)
                if not operation:
                    self.query_error = detail
                self.show_notice(_("Authorization was cancelled or denied.") if "NotAuthorized" in detail else _("Could not complete the request. %(detail)s") % {"detail": detail})
            self.render()
        self.connection.call(BUS, PATH, INTERFACE, method, parameters, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION if operation else Gio.DBusCallFlags.NONE, 150000, None, done, None)

    def receive(self, snapshot):
        if not view.valid_snapshot(snapshot):
            self.show_notice(_("The service returned an unsupported status format."))
            return
        if snapshot.get("sequence", 0) < self.snapshot.get("sequence", 0):
            return
        last = snapshot.get("last_application") or {}
        if not self.panel_mode and not self.options.demo and not self.initial_snapshot and view.new_failure(snapshot, self.seen_application):
            notification = Gio.Notification.new(_("Failed to apply CPU settings"))
            notification.set_body(_("Open the panel to inspect the error and current readings."))
            notification.set_default_action("app.open-status")
            self.send_notification("application-failed", notification)
        self.seen_application = last.get("id")
        self.initial_snapshot = False
        self.snapshot = snapshot
        self.query_error = ""
        if not self.dirty and not self.busy:
            self.load_editor()
        self.render()

    def watch_runtime(self):
        for monitor in self.monitors:
            monitor.cancel()
        self.monitors = []
        target = RUNTIME if RUNTIME.is_dir() else RUNTIME.parent
        try:
            monitor = Gio.File.new_for_path(str(target)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            monitor.connect("changed", self.file_changed)
            self.monitors.append(monitor)
        except GLib.Error:
            pass

    def file_changed(self, _monitor, file, other, _event):
        names = {item.get_basename() for item in (file, other) if item}
        if "cpu-power-limits" in names:
            self.watch_runtime()
        if "status.json" not in names:
            return
        try:
            path = RUNTIME / "status.json"
            if path.stat().st_size < 65536:
                self.receive(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass



def demo_snapshot(state):
    from cpu_power_limits import POWER_KEYS, DOMAINS, encode_offset, decode_offset
    config = dict(zip(POWER_KEYS, ("100", "135", "35", "45")))
    config.update(CORE_OFFSET="150", CACHE_OFFSET="150", IGPU_OFFSET="80")
    expected = {"PL1": 100, "PL2": 135, **{k: float(decode_offset(encode_offset(v))) for k, v in config.items() if k in DOMAINS}}
    readings = {**expected, "UNCORE_OFFSET": 0}
    if state == "mismatch":
        readings["PL1"] = 80
    if state == "failed":
        readings["CORE_OFFSET"] = 0
    if state == "unavailable":
        readings = dict.fromkeys(readings)
    return {"version": 1, "sequence": time.time_ns(), "checked_at": "2026-10-26T17:32:08+00:00",
            "profile": "ac", "source": "temporary" if state == "temporary" else "saved",
            "restoration_pending": False, "configuration": config, "saved_configuration": config,
            "readings": readings, "expected": expected, "errors": [],
            "last_application": {"id": "demo", "success": state != "failed", "timestamp": "2026-10-26T17:32:08+00:00",
                                 "error": "OC mailbox rejected domain 0: status 0x1" if state == "failed" else "",
                                 "configuration_saved": False}}



# StatusNotifierItem is the protocol consumed by AppIndicator hosts. Dbusmenu
# handles the menu protocol without loading GTK into the resident process.
SNI = "org.kde.StatusNotifierItem"
SNI_PATH = "/StatusNotifierItem"
SNI_XML = '''<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/>
<property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="IconName" type="s" access="read"/>
<property name="IconPixmap" type="a(iiay)" access="read"/>
<property name="OverlayIconName" type="s" access="read"/>
<property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
<property name="AttentionIconName" type="s" access="read"/>
<property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
<property name="AttentionMovieName" type="s" access="read"/>
<property name="WindowId" type="i" access="read"/>
<property name="IconThemePath" type="s" access="read"/>
<property name="Menu" type="o" access="read"/>
<property name="ItemIsMenu" type="b" access="read"/>
<property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<signal name="NewIcon"/><signal name="NewToolTip"/>
</interface></node>'''


class Indicator(StatusClient, Gio.Application):
    def __init__(self, options):
        super().__init__(application_id=indicator_id(options), flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.init_status(options)
        self.panel_process = None
        self.icon = "cpu-power-limits-unknown"
        self.title = _("Status unavailable")
        self.icon_dir = str(Path(__file__).resolve().parent / "icons")

    def do_startup(self):
        Gio.Application.do_startup(self)
        gi.require_version("Dbusmenu", "0.4")
        from gi.repository import Dbusmenu
        self.Dbusmenu = Dbusmenu
        self.hold()
        for name, callback in (("open-status", self.open_panel), ("quit", self.confirm_quit),
                               ("exit", self.quit), ("force-quit", self.force_quit)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_, callback=callback: callback())
            self.add_action(action)
        self.session = self.get_dbus_connection()
        self.registration = self.session.register_object(SNI_PATH, Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0],
                                                         self.item_method, self.item_property, None)
        self.menu = Dbusmenu.Server.new("/Menu")
        self.menu.set_icon_paths([self.icon_dir])
        self.watcher = Gio.bus_watch_name_on_connection(self.session, "org.kde.StatusNotifierWatcher",
                                                        Gio.BusNameWatcherFlags.NONE, self.watcher_ready, None)
        self.render()
        if self.options.demo:
            self.receive(demo_snapshot(self.options.demo_state))
        else:
            self.watch_runtime()
            self.file_changed(None, Gio.File.new_for_path(str(RUNTIME / "status.json")), None, None)
            Gio.bus_get(Gio.BusType.SYSTEM, None, self.bus_ready, None)

    def do_activate(self):
        if not (self.first_activation and self.options.autostart):
            self.open_panel()
        self.first_activation = False

    def watcher_ready(self, connection, name, _owner):
        def done(conn, result, _data):
            try:
                conn.call_finish(result)
            except GLib.Error as error:
                print("Could not register status indicator: " + error.message, file=sys.stderr)
        connection.call(name, "/StatusNotifierWatcher", name, "RegisterStatusNotifierItem",
                        GLib.Variant("(s)", (self.get_application_id(),)), None,
                        Gio.DBusCallFlags.NO_AUTO_START, 5000, None, done, None)

    def item_property(self, _connection, _sender, _path, _interface, name):
        values = {"Category": ("s", "Hardware"), "Id": ("s", "cpu-power-limits"),
                  "Title": ("s", "CPU Power Limits"), "Status": ("s", "Active"),
                  "IconName": ("s", self.icon), "IconPixmap": ("a(iiay)", []),
                  "OverlayIconName": ("s", ""), "OverlayIconPixmap": ("a(iiay)", []),
                  "AttentionIconName": ("s", ""), "AttentionIconPixmap": ("a(iiay)", []),
                  "AttentionMovieName": ("s", ""), "WindowId": ("i", 0), "IconThemePath": ("s", self.icon_dir),
                  "Menu": ("o", "/Menu"), "ItemIsMenu": ("b", True),
                  "ToolTip": ("(sa(iiay)ss)", (self.icon, [], "CPU Power Limits", self.title))}
        return GLib.Variant(*values[name])

    def item_method(self, _connection, _sender, _path, _interface, method, _parameters, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.open_panel()
        invocation.return_value(None)

    def open_panel(self):
        if send_action(panel_id(self.options), "open-status") or self.panel_process:
            return
        command = [sys.executable, str(Path(__file__).resolve()), "--panel"]
        if self.options.demo:
            command.extend(("--demo", "--demo-state", self.options.demo_state))
        try:
            self.panel_process = Gio.Subprocess.new(command, Gio.SubprocessFlags.NONE)
            self.panel_process.wait_async(None, self.panel_closed, None)
        except GLib.Error as error:
            self.show_notice(str(error))

    def panel_closed(self, process, result, _data):
        process.wait_finish(result)
        self.panel_process = None

    def confirm_quit(self):
        if not self.busy and not send_action(panel_id(self.options), "quit"):
            if self.panel_process:
                # The child may still be starting and not yet own its bus name.
                GLib.timeout_add(100, self.retry_quit)
            else:
                self.quit()

    def retry_quit(self):
        self.confirm_quit()
        return GLib.SOURCE_REMOVE

    def force_quit(self):
        if not send_action(panel_id(self.options), "force-quit") and self.panel_process:
            self.panel_process.force_exit()
        self.quit()

    def change(self, operation):
        # Let an open editor confirm before discarding unapplied edits.
        if not send_action(panel_id(self.options), operation):
            self.request(operation, {})

    def load_editor(self):
        pass

    def show_notice(self, text):
        notification = Gio.Notification.new("CPU Power Limits")
        notification.set_body(text)
        notification.set_default_action("app.open-status")
        self.send_notification("request-result", notification)

    def render(self):
        state, title = view.overall(self.snapshot, self.service_error)
        if self.query_error:
            state, title = "unavailable", _("Could not refresh status")
        if self.busy or self.service_busy:
            state, title = "applying", _("Applying configuration…")
        icons = {"verified": "ok", "warning": "warning", "error": "error", "unavailable": "unknown", "applying": "applying"}
        self.icon = "cpu-power-limits-" + icons[state]
        self.title = title
        root = self.Dbusmenu.Menuitem.new()
        def item(text, action=None, sensitive=True):
            entry = self.Dbusmenu.Menuitem.new()
            entry.property_set("label", text)
            entry.property_set_bool("enabled", sensitive and action is not None)
            if action:
                entry.connect("item-activated", lambda *_: action())
            root.child_append(entry)
        def separator():
            entry = self.Dbusmenu.Menuitem.new()
            entry.property_set("type", "separator")
            root.child_append(entry)
        item("CPU Power Limits")
        item(title)
        readings = self.snapshot.get("readings", {})
        profile = _("AC") if self.snapshot.get("profile") == "ac" else _("Battery") if self.snapshot.get("profile") == "battery" else _("Unknown power source")
        item(_("%(profile)s · %(pl1)s / %(pl2)s W") % {"profile": profile, "pl1": view.number(readings.get("PL1")), "pl2": view.number(readings.get("PL2"))})
        item(_("Last checked: %(time)s") % {"time": view.local_time(self.snapshot.get("checked_at"))})
        temporary = self.snapshot.get("source") == "temporary"
        if temporary:
            item(_("Temporary configuration active"))
        separator()
        busy = self.busy or self.service_busy
        item(_("Open panel"), self.open_panel)
        item(_("Refresh"), self.request, not busy)
        item(_("Reapply"), lambda: self.change("reapply"), not busy)
        if temporary:
            item(_("End test"), lambda: self.change("end-test"), not busy)
        separator()
        item(_("Quit"), self.confirm_quit, not self.busy)
        self.menu.set_root(root)
        for signal in ("NewIcon", "NewToolTip"):
            self.session.emit_signal(None, SNI_PATH, SNI, signal, None)

    def do_shutdown(self):
        self.close_status()
        Gio.bus_unwatch_name(self.watcher)
        self.session.unregister_object(self.registration)
        Gio.Application.do_shutdown(self)


def parse_options():
    locale.setlocale(locale.LC_ALL, "")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--panel", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--quit", action="store_true", help="Stop this user's panel and indicator")
    parser.add_argument("--demo", action="store_true", help="Preview with sample data; never contacts hardware")
    parser.add_argument("--demo-state", choices=("verified", "mismatch", "failed", "temporary", "unavailable"), default="verified")
    parser.add_argument("--demo-tab", choices=("status", "configuration"))
    parser.add_argument("--screenshot", help="Save a demo screenshot and exit")
    options = parser.parse_args()
    if options.screenshot and not options.demo:
        parser.error("--screenshot requires --demo")
    if options.screenshot or options.demo_tab:
        options.panel = True
    return options


# Exit the resident branch before importing GTK or the hardware controller.
if __name__ == "__main__":
    options = parse_options()
    if options.quit:
        send_action(panel_id(options), "force-quit")
        send_action(indicator_id(options), "force-quit")
        sys.exit(0)
    if not options.panel:
        sys.exit(Indicator(options).run([sys.argv[0]]))


gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Pango
from cpu_power_limits import Error, POWER_KEYS, DOMAINS, encode_offset, decode_offset
from cpu_power_limits_control import validate_configuration

def label(text="", css=None):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(True)
    widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if css:
        widget.get_style_context().add_class(css)
    return widget


def box(vertical=True, spacing=10):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def button(text, callback, primary=False):
    widget = Gtk.Button.new_with_label(text)
    widget.connect("clicked", callback)
    if primary:
        widget.get_style_context().add_class("suggested-action")
    return widget


class Desktop(StatusClient, Gtk.Application):
    def __init__(self, options):
        super().__init__(application_id=panel_id(options), flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.init_status(options, panel=True)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", lambda *_: self.confirm_quit())
        self.add_action(action)
        icon_dir = str(Path(__file__).resolve().parent / "icons")
        Gtk.IconTheme.get_default().append_search_path(icon_dir)
        force = Gio.SimpleAction.new("force-quit", None)
        force.connect("activate", lambda *_: self.quit())
        self.add_action(force)
        open_status = Gio.SimpleAction.new("open-status", None)
        open_status.connect("activate", lambda *_: (self.stack.set_visible_child_name("status"), self.activate()))
        self.add_action(open_status)
        for name in ("reapply", "end-test"):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_, name=name: self.change(name))
            self.add_action(action)
        self.build_window()
        self.render()
        if self.options.demo:
            self.receive(demo_snapshot(self.options.demo_state))
        else:
            Gio.bus_get(Gio.BusType.SYSTEM, None, self.bus_ready, None)
            self.watch_runtime()

    def do_activate(self):
        if not (self.first_activation and self.options.autostart):
            self.window.show_all()
            self.render()
            self.window.present()
            if self.connection and not self.options.demo:
                self.request()
        self.first_activation = False
        if self.options.demo_tab:
            self.stack.set_visible_child_name(self.options.demo_tab)
        if self.options.screenshot:
            GLib.timeout_add(700, self.take_screenshot)

    def build_window(self):
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("CPU Power Limits")
        self.window.set_icon_name("cpu-power-limits")
        self.window.set_default_size(620, 780)
        self.window.connect("delete-event", self.close_window)
        header = Gtk.HeaderBar(title="CPU Power Limits", subtitle=_("Power limits & voltage offsets"))
        header.set_show_close_button(True)
        self.window.set_titlebar(header)
        root = box()
        self.window.add(root)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER, margin_top=8)
        root.pack_start(switcher, False, False, 0)
        self.notice = Gtk.InfoBar(show_close_button=True, no_show_all=True)
        self.notice_text = label()
        self.notice.get_content_area().add(self.notice_text)
        self.notice.connect("response", lambda bar, *_: bar.hide())
        root.pack_start(self.notice, False, False, 0)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.add(self.stack)
        root.pack_start(scroll, True, True, 0)
        self.status = box(spacing=12)
        self.status.set_margin_top(20)
        self.status.set_margin_bottom(20)
        self.status.set_margin_start(20)
        self.status.set_margin_end(20)
        self.stack.add_titled(self.status, "status", _("Status"))
        self.status_title = label(_("Loading…"), "section-title")
        self.status.pack_start(self.status_title, False, False, 0)
        self.checked = label("", "dim-label")
        self.status.pack_start(self.checked, False, False, 0)
        self.last_attempt = label("", "dim-label")
        self.status.pack_start(self.last_attempt, False, False, 0)
        self.context = label("", "dim-label")
        self.status.pack_start(self.context, False, False, 0)
        self.temp_status = box()
        self.temp_status.pack_start(label(_("Temporary configuration active until restart")), False, False, 0)
        self.end_status = button(_("End test"), lambda *_: self.change("end-test"))
        self.temp_status.pack_start(self.end_status, False, False, 0)
        self.status.pack_start(self.temp_status, False, False, 0)
        self.cards = {}
        for title, keys in [(_("Power limits"), ("PL1", "PL2")), (_("Undervolt"), tuple(DOMAINS))]:
            section = box(spacing=10)
            section.pack_start(label(title, "section-title"), False, False, 0)
            grid = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True, halign=Gtk.Align.CENTER)
            for index, key in enumerate(keys):
                grid.attach(self.metric_card(key), index % 2, index // 2, 1, 1)
            section.pack_start(grid, False, False, 0)
            self.status.pack_start(section, False, False, 0)
        self.status.pack_start(label(_("Readings are snapshots. Refresh to check for later hardware changes."), "dim-label"), False, False, 0)
        self.errors = Gtk.Expander(label=_("Errors and technical details"))
        self.error_text = label()
        self.error_text.set_selectable(True)
        self.errors.add(self.error_text)
        self.status.pack_start(self.errors, False, False, 0)
        actions = box(False, 8)
        actions.set_halign(Gtk.Align.END)
        actions.pack_start(button(_("Copy diagnostics"), self.copy_diagnostics), False, False, 0)
        self.refresh_button = button(_("Refresh"), lambda *_: self.request())
        self.reapply_button = button(_("Reapply"), lambda *_: self.change("reapply"))
        actions.pack_start(self.refresh_button, False, False, 0)
        actions.pack_start(self.reapply_button, False, False, 0)
        self.status.pack_start(actions, False, False, 0)
        self.build_configuration()
        self.style()
        settings = Gtk.Settings.get_default()
        settings.connect("notify::gtk-theme-name", lambda *_: self.style())
        settings.connect("notify::gtk-application-prefer-dark-theme", lambda *_: self.style())

    def style(self):
        settings = Gtk.Settings.get_default()
        dark = settings.get_property("gtk-application-prefer-dark-theme") or "dark" in settings.get_property("gtk-theme-name").lower()
        good, warning = ("#78d49c", "#e9bc65") if dark else ("#197444", "#936000")
        css = f"""
        .section-title {{ font-weight: 600; }}
        .metric-card {{ background-color: @theme_base_color; border: 1px solid alpha(@theme_fg_color,0.08); border-radius: 16px; padding: 12px 15px; }}
        .metric-value {{ font-size: 28px; font-weight: 500; }}
        .power-value {{ font-size: 38px; }}
        .metric-value.verified {{ color: {good}; }}
        .metric-value.warning {{ color: {warning}; }}
        .metric-value.unmanaged, .metric-value.unavailable {{ color: @theme_fg_color; }}
        .metric-unit, .metric-foot {{ font-size: 12px; }}
        .metric-expected {{ font-size: 12px; color: @theme_fg_color; }}
        """
        if hasattr(self, "css"):
            Gtk.StyleContext.remove_provider_for_screen(Gdk.Screen.get_default(), self.css)
        self.css = Gtk.CssProvider()
        self.css.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def metric_card(self, key):
        card = box(spacing=3)
        card.set_size_request(190, 118)
        card.get_style_context().add_class("metric-card")
        top = box(False)
        top.pack_start(label(view.LABELS[key]), True, True, 0)
        icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic", Gtk.IconSize.MENU)
        top.pack_end(icon, False, False, 0)
        card.pack_start(top, False, False, 0)
        values = box(False, 4)
        value = label("—", "metric-value")
        value.set_line_wrap(False)
        if key in ("PL1", "PL2"):
            value.get_style_context().add_class("power-value")
        unit = label("W" if key in ("PL1", "PL2") else "mV", "metric-unit")
        unit.set_valign(Gtk.Align.BASELINE)
        value.set_valign(Gtk.Align.BASELINE)
        values.pack_start(value, False, False, 0)
        values.pack_start(unit, False, False, 0)
        card.pack_start(values, True, True, 0)
        expected = label("", "metric-expected")
        card.pack_start(expected, False, False, 0)
        foot = label("", "metric-foot")
        card.pack_end(foot, False, False, 0)
        self.cards[key] = (value, unit, expected, foot, icon, card)
        return card

    def build_configuration(self):
        self.editor = box(spacing=14)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.editor, "set_margin_" + side)(20)
        self.stack.add_titled(self.editor, "configuration", _("Configuration"))
        self.edit_source = label("", "dim-label")
        self.editor.pack_start(self.edit_source, False, False, 0)
        self.end_editor = button(_("End test"), lambda *_: self.change("end-test"))
        self.editor.pack_start(self.end_editor, False, False, 0)
        self.editor.pack_start(label(_("Power limits"), "section-title"), False, False, 0)
        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        self.entries = {}
        self.managed = {}
        grid.attach(label(_("Power source")), 0, 0, 1, 1)
        grid.attach(label("PL1 (W)"), 1, 0, 1, 1)
        grid.attach(label("PL2 (W)"), 2, 0, 1, 1)
        for row, (prefix, text) in enumerate((("AC", _("AC")), ("BATTERY", _("Battery"))), 1):
            grid.attach(label(text), 0, row, 1, 1)
            for col in (1, 2):
                key = f"{prefix}_PL{col}"
                entry = self.entry(key)
                grid.attach(entry, col, row, 1, 1)
        self.editor.pack_start(grid, False, False, 0)
        self.editor.pack_start(label(_("Undervolt"), "section-title"), False, False, 0)
        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        for row, key in enumerate(DOMAINS):
            check = Gtk.CheckButton.new_with_label(_("Manage %(domain)s") % {"domain": view.LABELS[key]})
            check.connect("toggled", self.edited)
            self.managed[key] = check
            grid.attach(check, 0, row, 1, 1)
            grid.attach(self.entry(key), 1, row, 1, 1)
            grid.attach(label("mV"), 2, row, 1, 1)
        self.editor.pack_start(grid, False, False, 0)
        self.editor.pack_start(label(_("Enter undervolt magnitudes; applied offsets are negative. Unchecked domains are preserved. Set 0 to reset a managed offset."), "dim-label"), False, False, 0)
        rounding = Gtk.Expander(label=_("Expected hardware rounding"))
        self.rounding_text = label("", "dim-label")
        rounding.add(self.rounding_text)
        self.editor.pack_start(rounding, False, False, 0)
        self.validation = label()
        self.editor.pack_start(self.validation, False, False, 0)
        self.edit_note = label("", "dim-label")
        self.editor.pack_start(self.edit_note, False, False, 0)
        actions = box(False, 8)
        self.discard_button = button(_("Discard changes"), self.discard)
        self.test_button = button(_("Test for this boot"), lambda *_: self.change("test"))
        self.save_button = button(_("Save and apply"), lambda *_: self.change("save"), True)
        for child in (self.discard_button, self.test_button, self.save_button):
            actions.pack_start(child, False, False, 0)
        self.editor.pack_start(actions, False, False, 0)

    def entry(self, key):
        entry = Gtk.Entry(width_chars=9, max_width_chars=12, max_length=64, input_purpose=Gtk.InputPurpose.NUMBER)
        entry.get_accessible().set_name(key)
        entry.connect("changed", self.edited)
        self.entries[key] = entry
        return entry

    def editor_data(self):
        data = {}
        for key, entry in self.entries.items():
            if key in self.managed and not self.managed[key].get_active():
                continue
            data[key] = locale.delocalize(entry.get_text().strip())
        validate_configuration(data)
        if any(float(data[key]) < 0 for key in DOMAINS if key in data):
            raise Error("Enter a non-negative undervolt magnitude")
        return data

    def edited(self, *_):
        if not self.loading:
            self.dirty = True
            self.update_editor()

    def update_editor(self):
        busy = self.busy or self.service_busy
        error = ""
        try:
            data = self.editor_data()
            self.rounding_text.set_text("\n".join(
                _("%(domain)s: %(value)s mV") % {"domain": view.LABELS[key], "value": view.number(float(decode_offset(encode_offset(value))))}
                for key, value in data.items() if key in DOMAINS))
        except (Error, ValueError) as exc:
            error = _("Enter positive whole watts with PL1 ≤ PL2, and undervolt magnitudes from 0 to 1000 mV.")
            self.validation.set_tooltip_text(str(exc))
        self.validation.set_text(error)
        for key, entry in self.entries.items():
            entry.set_sensitive(not busy and (key not in self.managed or self.managed[key].get_active()))
        for check in self.managed.values():
            check.set_sensitive(not busy)
        for child in (self.test_button, self.save_button):
            child.set_sensitive(not busy and not error and bool(self.snapshot))
        self.discard_button.set_sensitive(self.dirty and not busy)
        self.edit_note.set_text(_("Waiting for authorization or application…") if self.busy else
                                _("Changes not applied") if self.dirty else _("No unapplied changes"))

    def load_editor(self):
        self.loading = True
        data = self.snapshot.get("configuration") or self.snapshot.get("saved_configuration") or {}
        for key, entry in self.entries.items():
            entry.set_text(data.get(key, "0"))
        for key, check in self.managed.items():
            check.set_active(key in data)
        self.loading = False
        self.dirty = False
        self.update_editor()

    def discard(self, *_):
        self.load_editor()

    def render(self):
        snapshot = self.snapshot
        state, title = view.overall(snapshot, self.service_error) if snapshot else ("unavailable", _("Status unavailable"))
        if self.query_error:
            state, title = "unavailable", _("Could not refresh status")
        if self.busy:
            state, title = "applying", _("Waiting for authorization or application…")
        elif self.service_busy:
            state, title = "applying", _("Applying configuration…")
        busy = self.busy or self.service_busy
        self.status_title.set_text(title)
        self.checked.set_text(_("Last checked: %(time)s") % {"time": view.local_time(snapshot.get("checked_at"))})
        last = snapshot.get("last_application") or {}
        outcome = _("Succeeded") if last.get("success") else _("Saved; application failed") if last.get("configuration_saved") else _("Failed")
        self.last_attempt.set_text(_("Last application: %(time)s · %(result)s") % {"time": view.local_time(last.get("timestamp")), "result": outcome})
        self.last_attempt.set_visible(bool(last))
        temporary = snapshot.get("source") == "temporary"
        profile = _("AC") if snapshot.get("profile") == "ac" else _("Battery") if snapshot.get("profile") == "battery" else _("Unknown power source")
        source = _("Temporary configuration") if temporary else _("Saved configuration")
        self.context.set_text(_("%(profile)s · %(source)s") % {"profile": profile, "source": source})
        self.edit_source.set_text(_("Editing temporary configuration — active until restart") if temporary else _("Editing saved configuration"))
        self.temp_status.set_visible(temporary)
        self.end_editor.set_visible(temporary)
        self.end_status.set_sensitive(not busy)
        self.end_editor.set_sensitive(not busy)
        for key, (value, unit, expected, foot, image, card_widget) in self.cards.items():
            item = view.card(snapshot, key)
            if self.query_error:
                item.update(value=None, state="unavailable", show_expected=item["expected"] is not None)
            context = value.get_style_context()
            for name in ("verified", "warning", "unmanaged", "unavailable"):
                context.remove_class(name)
            context.add_class(item["state"])
            value.set_text(view.number(item["value"]))
            unit.set_visible(item["value"] is not None)
            expected.set_text(_("Expected %(value)s %(unit)s") % {"value": view.number(item["expected"]), "unit": item["unit"]} if item["show_expected"] else "")
            expected.set_visible(item["show_expected"])
            description = _("Read unavailable") if item["state"] == "unavailable" else _("Not managed") if item["state"] == "unmanaged" else _("Value mismatch") if item["state"] == "warning" else _("Sustained power") if key == "PL1" else _("Short-term power") if key == "PL2" else _("Verified")
            foot.set_text(description)
            image.set_from_icon_name("dialog-warning-symbolic" if item["state"] in ("warning", "unavailable") else "list-remove-symbolic" if item["state"] == "unmanaged" else "emblem-ok-symbolic", Gtk.IconSize.MENU)
            card_widget.get_accessible().set_name(_("%(setting)s: %(value)s %(unit)s. %(state)s") % {"setting": view.LABELS[key], "value": view.number(item["value"]), "unit": item["unit"], "state": description})
        details = [last.get("error", ""), self.service_error, self.query_error]
        details.extend(error.get("detail", "") for error in snapshot.get("errors", []))
        details = list(dict.fromkeys(detail for detail in details if detail))
        if snapshot.get("restoration_pending"):
            details.insert(0, _("Restoration is pending. Reapply to retry."))
        self.error_text.set_text("\n\n".join(details))
        self.errors.set_visible(bool(details))
        if state == "error":
            self.errors.set_expanded(True)
        self.refresh_button.set_sensitive(not busy)
        self.reapply_button.set_sensitive(not busy and bool(snapshot.get("configuration")))
        self.update_editor()

    def show_notice(self, text):
        self.notice_text.set_text(text)
        self.notice.get_content_area().show_all()
        self.notice.show()

    def change(self, operation):
        if self.busy or self.service_busy:
            return
        try:
            data = self.editor_data() if operation in ("save", "test") else {}
        except (Error, ValueError):
            self.update_editor()
            return
        if self.dirty and operation in ("end-test", "reapply") and not self.confirm_discard():
            return
        if self.options.demo:
            self.show_notice(_("Preview only. No configuration or hardware was changed."))
            return
        self.request(operation, data)

    def copy_diagnostics(self, *_args):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(json.dumps(self.snapshot, indent=2, ensure_ascii=False), -1)
        self.show_notice(_("Diagnostics copied to the clipboard."))

    def confirm_discard(self):
        if not self.dirty:
            return True
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   text=_("Discard unapplied changes?"))
        dialog.format_secondary_text(_("Your edits have not been saved or applied."))
        dialog.add_button(_("Keep editing"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("Discard changes"), Gtk.ResponseType.OK)
        answer = dialog.run()
        dialog.destroy()
        if answer == Gtk.ResponseType.OK:
            self.load_editor()
            return True
        return False

    def close_window(self, *_args):
        if self.busy:
            self.show_notice(_("Please wait for the current operation before closing the panel."))
        elif self.confirm_discard():
            self.quit()
        return True

    def confirm_quit(self):
        if self.busy:
            self.show_notice(_("Please wait for the current operation before closing the panel."))
        elif self.confirm_discard():
            send_action(indicator_id(self.options), "exit", wait=False)
            self.quit()

    def do_shutdown(self):
        self.close_status()
        Gtk.Application.do_shutdown(self)

    def take_screenshot(self):
        window = self.window.get_window()
        pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, self.window.get_allocated_width(), self.window.get_allocated_height())
        pixbuf.savev(self.options.screenshot, "png", [], [])
        self.quit()
        return GLib.SOURCE_REMOVE


if __name__ == "__main__":
    sys.exit(Desktop(options).run([sys.argv[0]]))
