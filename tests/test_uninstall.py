"""Offline removal and mocked service lifecycle; never touches host services."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        staged = subprocess.run([str(REPO / 'install.sh'), '--root', str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(staged.returncode, 0, staged.stderr)

    def uninstall(self, *args):
        return subprocess.run([str(REPO / 'uninstall.sh'), '--root', str(self.root), *args],
                              capture_output=True, text=True, timeout=5)

    def test_repeated_removal_preserves_configuration_and_unrelated_files(self):
        config = self.root / 'etc/cpu-power-limits.conf'
        contents = config.read_bytes()
        unrelated = self.root / 'etc/udev/rules.d/90-other.rules'
        unrelated.write_text('unrelated fixture')
        for _ in range(2):
            result = self.uninstall()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('no hardware or services accessed', result.stdout)
            self.assertEqual(config.read_bytes(), contents)
            self.assertEqual(unrelated.read_text(), 'unrelated fixture')
            for name in ('etc/systemd/system/cpu-power-limits.service',
                         'etc/udev/rules.d/91-cpu-power-limits.rules',
                         'usr/lib/systemd/system-sleep/cpu-power-limits',
                         'usr/local/bin/cpu-power-limits', 'usr/local/libexec/cpu-power-limits'):
                path = self.root / name
                self.assertFalse(path.exists(), name)
                self.assertFalse(path.is_symlink(), name)

    def test_purge_removes_configuration_idempotently(self):
        for _ in range(2):
            self.assertEqual(self.uninstall('--purge-config').returncode, 0)
            self.assertFalse((self.root / 'etc/cpu-power-limits.conf').exists())

    def test_offline_restore_and_invalid_arguments_fail_without_removal(self):
        for args in (['--restore-thermald'], ['--unknown'], ['--root'], ['--root', '/']):
            with self.subTest(args=args):
                result = self.uninstall(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((self.root / 'usr/local/libexec/cpu-power-limits').exists())

    def run_lifecycle(self, purge='false', restore='false', **variables):
        harness = r'''
source "$1/uninstall.sh"
systemctl() {
    printf 'systemctl %s\n' "$*" >> "$TEST_LOG"
    [[ "$1" != "${FAIL_COMMAND:-none}" ]] || return 1
    if [[ "$1" == show ]]; then printf '%s\n' "${LOAD_STATE:-loaded}"; fi
}
udevadm() { printf 'udevadm %s\n' "$*" >> "$TEST_LOG"; }
remove_files() { printf 'remove_files purge=%s\n' "$2" >> "$TEST_LOG"; }
uninstall_live "$2" "$3"
'''
        log = self.root / 'commands'
        log.write_text('')
        result = subprocess.run(['bash', '-c', harness, 'test', str(REPO), purge, restore],
                                env=dict(os.environ, TEST_LOG=str(log), **variables),
                                capture_output=True, text=True, timeout=5)
        return result, log.read_text()

    def test_live_stop_remove_reload_order_and_thermald_preservation(self):
        result, commands = self.run_lifecycle()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(commands.index('disable --now'), commands.index('remove_files'))
        self.assertLess(commands.index('remove_files'), commands.index('daemon-reload'))
        self.assertIn('udevadm control --reload-rules', commands)
        self.assertNotIn('thermald', commands)
        self.assertIn('purge=false', commands)

    def test_missing_service_still_removes_files_and_reloads(self):
        result, commands = self.run_lifecycle(LOAD_STATE='not-found')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('disable --now', commands)
        self.assertIn('remove_files', commands)
        self.assertIn('daemon-reload', commands)

    def test_explicit_thermald_restore_and_purge(self):
        result, commands = self.run_lifecycle(purge='true', restore='true')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('purge=true', commands)
        self.assertIn('unmask thermald.service dbus-org.freedesktop.thermald.service', commands)
        self.assertIn('enable --now thermald.service', commands)
        self.assertLess(commands.index('daemon-reload'), commands.index('unmask'))

    def test_systemd_connection_or_stop_failure_prevents_removal(self):
        for command in ('show', 'disable'):
            with self.subTest(command=command):
                result, commands = self.run_lifecycle(FAIL_COMMAND=command)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('remove_files', commands)
                self.assertNotIn('daemon-reload', commands)


if __name__ == '__main__':
    unittest.main()
