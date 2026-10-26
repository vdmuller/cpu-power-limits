"""Opt-in real GTK checks; all values come from the built-in demo fixture."""

import os
import subprocess
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(os.environ.get("CPU_POWER_LIMITS_GUI_TESTS") == "1", "Opt-in GTK test; use a display or Xvfb")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from argparse import Namespace
        from cpu_power_limits_gui import Desktop, Gtk, Adw
        cls.Gtk = Gtk
        cls.Adw = Adw
        options = Namespace(demo=True, demo_state="verified", autostart=False, demo_tab=None, screenshot=None)
        cls.app = Desktop(options)
        cls.app.register(None)
        cls.app.activate()

    @classmethod
    def tearDownClass(cls):
        cls.app.window.destroy()
        cls.app.quit()

    def respond_to_discard(self, response_label):
        # Click the native dialog button so its response and close behavior run.
        def find(widget):
            if isinstance(widget, self.Gtk.Button) and widget.get_label() == response_label:
                return widget
            child = widget.get_first_child()
            while child:
                result = find(child)
                if result is not None:
                    return result
                child = child.get_next_sibling()
            return None
        button = find(self.app.discard_dialog)
        self.assertIsNotNone(button, response_label)
        button.emit("clicked")

    def test_navigation_and_editing_never_reach_hardware(self):
        app = self.app
        with patch.object(app, "request") as request:
            app.stack.set_visible_child_name("configuration")
            self.assertEqual(app.stack.get_visible_child_name(), "configuration")
            # The switcher is outside the stack and remains visible in both tabs.
            switcher = app.switcher
            self.assertTrue(switcher.get_visible())
            app.entries["AC_PL1"].set_text("90")
            self.assertTrue(app.dirty)
            self.assertTrue(app.save_button.get_sensitive())
            app.test_button.emit("clicked")
            request.assert_not_called()  # Demo actions cannot touch hardware.
            app.discard()
            self.assertEqual(app.entries["AC_PL1"].get_text(), "100")
            app.entries["AC_PL1"].set_text("200")
            self.assertFalse(app.save_button.get_sensitive())
            app.discard()
            app.stack.set_visible_child_name("status")
            self.assertTrue(switcher.get_visible())
            self.assertEqual(app.stack.get_visible_child_name(), "status")
            app.copy_diagnostics()
            self.assertIn("copied", app.notice_text.get_text())

    def test_application_feedback_text_is_visible_and_can_be_reopened(self):
        app = self.app
        for text in ("Temporary configuration applied for this boot.", "Application failed. See Status for details."):
            app.notice.set_visible(False)
            app.show_notice(text)
            self.assertTrue(app.notice.get_visible())
            self.assertTrue(app.notice_text.get_visible())
            self.assertTrue(app.notice_text.get_mapped())
            self.assertEqual(app.notice_text.get_text(), text)

    def test_end_test_applies_saved_configuration_in_both_tabs(self):
        from cpu_power_limits_gui import demo_snapshot
        app = self.app
        app.receive(demo_snapshot("temporary"))
        self.assertTrue(app.end_status.get_visible())
        self.assertTrue(app.end_editor.get_visible())
        self.assertEqual(app.end_status.get_label(), "End test")
        self.assertEqual(app.end_editor.get_label(), "End test")
        with patch.object(app, "change") as change:
            app.end_status.emit("clicked")
            app.end_editor.emit("clicked")
            self.assertEqual([call.args for call in change.call_args_list], [("end-test",), ("end-test",)])
        app.receive(demo_snapshot("verified"))
        self.assertFalse(app.temp_status.get_visible())
        self.assertFalse(app.end_editor.get_visible())

    def test_failure_card_has_warning_and_expected_value(self):
        from cpu_power_limits_gui import demo_snapshot
        app = self.app
        app.receive(demo_snapshot("failed"))
        value, _unit, expected, _foot, _image, _card = app.cards["CORE_OFFSET"]
        self.assertTrue(value.has_css_class("warning"))
        self.assertTrue(expected.get_visible())
        self.assertIn("150", expected.get_text())
        app.receive(demo_snapshot("verified"))
        self.assertTrue(value.has_css_class("verified"))
        self.assertFalse(expected.get_visible())

    def test_close_respects_pending_operation_and_unapplied_edits(self):
        app = self.app
        with patch.object(app, "quit") as quit_app:
            app.busy = True
            try:
                app.close_window()
                quit_app.assert_not_called()
            finally:
                app.busy = False
            app.entries["AC_PL1"].set_text("90")
            app.close_window()
            quit_app.assert_not_called()
            self.respond_to_discard("Keep editing")
            self.assertTrue(app.dirty)
            self.assertEqual(app.entries["AC_PL1"].get_text(), "90")
            quit_app.assert_not_called()
            app.close_window()
            self.respond_to_discard("Discard changes")
            self.assertFalse(app.dirty)
            quit_app.assert_called_once()

    def test_reapply_waits_for_discard_confirmation(self):
        app = self.app
        app.entries["AC_PL1"].set_text("90")
        with patch.object(app, "request") as request:
            app.change("reapply")
            request.assert_not_called()
            self.respond_to_discard("Keep editing")
            self.assertTrue(app.dirty)
            request.assert_not_called()
            app.change("reapply")
            self.respond_to_discard("Discard changes")
            self.assertFalse(app.dirty)
            self.assertIn("Preview only", app.notice_text.get_text())
            request.assert_not_called()  # Demo mode remains isolated after confirmation.

    def test_native_adwaita_window_and_appearance(self):
        app = self.app
        self.assertIsInstance(app, self.Adw.Application)
        self.assertIsInstance(app.window, self.Adw.ApplicationWindow)
        self.assertIsInstance(app.stack, self.Adw.ViewStack)
        manager = app.get_style_manager()
        original = manager.get_color_scheme()
        try:
            manager.set_color_scheme(self.Adw.ColorScheme.FORCE_DARK)
            self.assertTrue(manager.get_dark())
            manager.set_color_scheme(self.Adw.ColorScheme.FORCE_LIGHT)
            self.assertFalse(manager.get_dark())
        finally:
            manager.set_color_scheme(original)

    def test_demo_screenshot_uses_gtk4_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "status.png"
            command = ["dbus-run-session", "--", sys.executable,
                       str(Path(__file__).resolve().parents[1] / "cpu_power_limits_gui.py"),
                       "--demo", "--dark", "--demo-tab", "status", "--screenshot", str(screenshot)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                    env={**os.environ, "GSK_RENDERER": "cairo", "GTK_A11Y": "none"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(screenshot.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_failed_save_before_persistence_preserves_edits(self):
        import json
        from unittest.mock import Mock
        from cpu_power_limits_gui import demo_snapshot, GLib
        app = self.app
        app.entries["AC_PL1"].set_text("90")
        data = app.editor_data()
        response = demo_snapshot("verified")
        response["last_application"].update(success=False, error="Permission denied", configuration_saved=False)
        connection = Mock()
        connection.call_finish.return_value = GLib.Variant("(s)", (json.dumps(response),))
        connection.call.side_effect = lambda *args: args[-2](connection, None, None)
        original = app.connection
        try:
            app.options.demo = False
            app.connection = connection
            with patch.object(app, "check_service"), patch.object(app, "watch_runtime"), patch.object(app, "send_notification"):
                app.request("save", data)
            self.assertTrue(app.dirty)
            self.assertEqual(app.entries["AC_PL1"].get_text(), "90")
        finally:
            app.options.demo = True
            app.connection = original
            app.discard()
            app.receive(demo_snapshot("verified"))


if __name__ == "__main__":
    unittest.main()
