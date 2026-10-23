"""Hardware and installer tests; fixtures never access the host's MSRs or services."""
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
import io
import fcntl
import os
from pathlib import Path
import pty
import select
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpu_power_limits as cpl

REPO = Path(__file__).resolve().parents[1]
CONFIG = "AC_PL1=80\nAC_PL2=110\nBATTERY_PL1=35\nBATTERY_PL2=45\n"


class FakeMailbox:
    def __init__(self, values=None):
        self.values = dict.fromkeys(range(5), 0)
        self.values.update(values or {})
        self.writes = []
        self.fail = None
        self.read_fail = set()

    def read(self, domain):
        if domain in self.read_fail:
            raise cpl.Error("firmware unavailable")
        return self.values[domain]

    def write(self, domain, bits):
        # Call the real guard even when exercising rollback with a fake device.
        if cpl.decode_offset(bits) > 0 or domain not in cpl.DOMAINS.values():
            raise cpl.Error("Refusing positive offset or unmanaged domain")
        self.writes.append((domain, bits))
        self.values[domain] = bits
        if self.fail == domain:
            self.fail = None
            raise cpl.Error("injected write failure after hardware changed")

    def close(self):
        pass


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.zone = self.root / "powercap/intel-rapl-mmio:7"
        self.zone.mkdir(parents=True)
        for name, value in {
            "name": "package-0", "constraint_3_name": "long_term",
            "constraint_8_name": "short_term", "constraint_3_power_limit_uw": "45000000",
            "constraint_8_power_limit_uw": "90000000", "enabled": "1",
            "constraint_3_max_power_uw": "55000000", "constraint_3_time_window_us": "28000000",
            "constraint_2_name": "peak_power", "constraint_2_power_limit_uw": "150000000",
        }.items():
            (self.zone / name).write_text(value + "\n")
        other = self.zone.parent / "intel-rapl:0"
        other.mkdir()
        (other / "constraint_0_power_limit_uw").write_text("123000000\n")
        self.supply = self.root / "power_supply/ADP0"
        self.supply.mkdir(parents=True)
        (self.supply / "type").write_text("Mains\n")
        (self.supply / "online").write_text("1\n")
        self.config = self.root / "limits.conf"
        self.config.write_text(CONFIG)
        self.env = {
            "CPU_POWER_LIMITS_POWERCAP_ROOT": str(self.zone.parent),
            "CPU_POWER_LIMITS_POWER_SUPPLY_ROOT": str(self.supply.parent),
            "CPU_POWER_LIMITS_LOCK_FILE": str(self.root / "lock"),
            "CPU_POWER_LIMITS_MSR_DEVICE": str(self.root / "missing-msr"),
            "NOTIFY_SOCKET": "",
        }
        self.limits = cpl.Limits(self.zone.parent)

    def run_cli(self, *args, mailbox=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), redirect_stdout(out), redirect_stderr(err):
            with patch.object(cpl, "Mailbox", return_value=mailbox) if mailbox else patch.object(
                    cpl, "Mailbox", side_effect=FileNotFoundError("simulated missing MSR")):
                code = cpl.main(["--config", str(self.config), *args])
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        return {p.relative_to(self.zone.parent): p.read_bytes()
                for p in self.zone.parent.rglob("*") if p.is_file()}


class ConfigTests(Fixture):
    def test_sign_normalization_quantization_and_zero(self):
        for positive, negative in [("150", "-150"), ("80", "-80"), ("0.1", "-0.1"), ("1000", "-1000")]:
            self.assertEqual(cpl.encode_offset(positive), cpl.encode_offset(negative))
            self.assertLessEqual(cpl.decode_offset(cpl.encode_offset(positive)), 0)
        self.assertEqual(cpl.decode_offset(cpl.encode_offset("150")), Decimal("-150.390625"))
        self.assertEqual(cpl.decode_offset(cpl.encode_offset("80")), Decimal("-80.078125"))
        self.assertEqual(cpl.encode_offset("0"), 0)

    def test_invalid_configuration_never_writes(self):
        before = self.snapshot()
        for addition in ("UNKNOWN=3", "CORE_MV=1", "CORE_OFFSET=nan", "CORE_OFFSET=inf",
                         "CORE_OFFSET=1000.1", "CORE_OFFSET=-1001", "CORE_OFFSET=1e2",
                         "CORE_OFFSET=$(touch secret)", "CORE_OFFSET=1\nCORE_OFFSET=2", "AC_PL1=3"):
            self.config.write_text(CONFIG + addition + "\n")
            with self.subTest(addition=addition), self.assertRaises(cpl.Error):
                self.run_cli()
            self.assertEqual(before, self.snapshot())
        for value in ("0", "-1", "1.5", "080", "111", "9223372036855", "9" * 80):
            self.config.write_text(CONFIG.replace("AC_PL1=80", "AC_PL1=" + value))
            with self.subTest(value=value), self.assertRaises(cpl.Error):
                cpl.read_config(self.config)

    def test_missing_required_key_and_duplicate(self):
        self.config.write_text(CONFIG.replace("BATTERY_PL2=45", ""))
        with self.assertRaisesRegex(cpl.Error, "Missing"):
            cpl.read_config(self.config)

    def test_comments_whitespace_and_decimal_offsets(self):
        self.config.write_text(CONFIG + "\n # offsets\n CORE_OFFSET = +12.5 # mV\nCACHE_OFFSET=-.5")
        settings = cpl.read_config(self.config)
        self.assertEqual(settings["CORE_OFFSET"], "+12.5")
        self.assertEqual(settings["CACHE_OFFSET"], "-.5")
        self.assertNotIn("IGPU_OFFSET", settings)
        self.assertEqual(self.run_cli("--validate")[0], 0)


class LimitsTests(Fixture):
    def test_profiles_and_untouched_constraints(self):
        before = self.snapshot()
        for online, expected in (("1", (80000000, 110000000)), ("0", (35000000, 45000000))):
            (self.supply / "online").write_text(online)
            code, out, _ = self.run_cli()
            self.assertEqual(code, 0, out)
            self.assertEqual(self.limits.read(), expected)
            self.assertIn("current=unavailable", out)
        for path, value in before.items():
            if path.name not in ("constraint_3_power_limit_uw", "constraint_8_power_limit_uw"):
                self.assertEqual((self.zone.parent / path).read_bytes(), value)

    def test_power_idempotence(self):
        self.run_cli()
        timestamps = [p.stat().st_mtime_ns for p in self.limits.files]
        self.run_cli()
        self.assertEqual(timestamps, [p.stat().st_mtime_ns for p in self.limits.files])

    def test_write_order_when_raising_and_lowering(self):
        writes = []
        original = Path.write_text
        def write(path, value, *args, **kwargs):
            writes.append(path)
            return original(path, value, *args, **kwargs)
        with patch.object(Path, "write_text", write):
            self.limits.write((100000000, 135000000))
            self.limits.write((35000000, 45000000))
        self.assertEqual(writes, [self.limits.files[1], self.limits.files[0],
                                  self.limits.files[0], self.limits.files[1]])

    def test_check_queries_without_changing_hardware(self):
        before = self.snapshot()
        mailbox = FakeMailbox()
        code, out, _ = self.run_cli("--check", mailbox=mailbox)
        self.assertEqual(code, 0)
        self.assertIn("PL1=45 W PL2=90 W", out)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(mailbox.writes, [])
        self.assertEqual(self.run_cli("--check", "--profile", "auto", mailbox=mailbox)[0], 1)

    def test_usb_pd_and_device_scope(self):
        (self.supply / "online").write_text("0")
        usb = self.supply.parent / "USB0"
        usb.mkdir()
        (usb / "type").write_text("USB_PD\n")
        (usb / "online").write_text("1\n")
        self.assertEqual(cpl.detect_profile(self.supply.parent), "ac")
        (usb / "scope").write_text("Device\n")
        self.assertEqual(cpl.detect_profile(self.supply.parent), "battery")

    def test_unknown_power_source_fails_before_writes(self):
        before = self.snapshot()
        for value in ("invalid", ""):
            (self.supply / "online").write_text(value)
            with self.assertRaises(cpl.Error):
                self.run_cli()
            self.assertEqual(before, self.snapshot())
        (self.supply / "type").unlink()
        with self.assertRaises(cpl.Error):
            cpl.detect_profile(self.supply.parent)

    def test_check_serializes_with_hardware_lock(self):
        with open(self.env["CPU_POWER_LIMITS_LOCK_FILE"], "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            proc = subprocess.Popen([sys.executable, str(REPO / "cpu_power_limits.py"),
                                     "--config", str(self.config), "--check"],
                                    env=dict(os.environ, **self.env), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    proc.wait(timeout=0.15)
                fcntl.flock(lock, fcntl.LOCK_UN)
                out, err = proc.communicate(timeout=3)
                self.assertEqual(proc.returncode, 0, err)
                self.assertIn("PL1=45 W", out)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()

    def test_real_cli_errors_and_complete_check_output(self):
        self.config.write_text(CONFIG + "CORE_OFFSET=150\n")
        result = subprocess.run([sys.executable, str(REPO / "cpu_power_limits.py"),
                                 "--config", str(self.config), "--check"],
                                env=dict(os.environ, **self.env), capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        for key in cpl.DOMAINS:
            self.assertIn(key, result.stdout)
        self.config.write_text("invalid")
        result = subprocess.run([sys.executable, str(REPO / "cpu_power_limits.py"),
                                 "--config", str(self.config), "--validate"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_ambiguous_mmio_and_invalid_reading(self):
        (self.zone / "name").write_text("other")
        with self.assertRaises(cpl.Error):
            cpl.Limits(self.zone.parent)
        (self.zone / "name").write_text("package-0")
        (self.zone / "constraint_9_name").write_text("long_term")
        with self.assertRaises(cpl.Error):
            cpl.Limits(self.zone.parent)
        for value in ("invalid", "9223372036854775808", "9" * 100):
            self.limits.files[0].write_text(value)
            with self.assertRaises(cpl.Error):
                self.limits.read()


class OffsetTests(Fixture):
    def test_optional_offsets_preserve_omitted_domains_and_explicit_zero(self):
        initial = {d: cpl.encode_offset("20") for d in range(5)}
        mailbox = FakeMailbox(initial)
        self.config.write_text(CONFIG + "CORE_OFFSET=150\nCACHE_OFFSET=-150\nIGPU_OFFSET=0\n")
        code, out, _ = self.run_cli(mailbox=mailbox)
        self.assertEqual(code, 0, out)
        self.assertEqual(mailbox.values[0], cpl.encode_offset("150"))
        self.assertEqual(mailbox.values[2], cpl.encode_offset("150"))
        self.assertEqual(mailbox.values[1], 0)
        self.assertEqual(mailbox.values[3], initial[3])
        self.assertEqual(mailbox.values[4], initial[4])
        self.assertTrue(all(cpl.decode_offset(bits) <= 0 for _, bits in mailbox.writes))
        writes = list(mailbox.writes)
        self.run_cli(mailbox=mailbox)
        self.assertEqual(mailbox.writes, writes)

    def test_all_domains_and_report(self):
        self.config.write_text(CONFIG + "".join(f"{key}=80\n" for key in cpl.DOMAINS))
        mailbox = FakeMailbox()
        self.assertEqual(self.run_cli(mailbox=mailbox)[0], 0)
        self.assertEqual(set(d for d, _ in mailbox.writes), {0, 1, 2, 3})
        code, out, _ = self.run_cli("--check", "--profile", "auto", mailbox=mailbox)
        self.assertEqual(code, 0)
        self.assertIn("normalized=-80 mV expected=-80.07812 mV", out)
        self.assertNotIn("MISMATCH", out)

    def test_unavailable_and_mismatched_offsets_print_all_results(self):
        self.config.write_text(CONFIG + "CORE_OFFSET=150\nIGPU_OFFSET=80\n")
        mailbox = FakeMailbox()
        mailbox.read_fail.add(1)
        before = self.snapshot()
        code, out, _ = self.run_cli("--check", "--profile", "auto", mailbox=mailbox)
        self.assertEqual(code, 1)
        for key in cpl.DOMAINS:
            self.assertIn(key, out)
        self.assertIn("firmware unavailable", out)
        self.assertIn("MISMATCH", out)
        self.assertEqual(before, self.snapshot())
        code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("simulated missing MSR", out)

    def test_configured_unavailable_domain_stops_before_power_write(self):
        self.config.write_text(CONFIG + "CORE_OFFSET=150\n")
        before = self.snapshot()
        with self.assertRaises(cpl.Error):
            self.run_cli()
        self.assertEqual(before, self.snapshot())

    def test_preflight_accepts_different_current_settings_without_applying(self):
        self.config.write_text(CONFIG + "CORE_OFFSET=150\n")
        mailbox = FakeMailbox()
        before = self.snapshot()
        self.assertEqual(self.run_cli("--preflight", mailbox=mailbox)[0], 0)
        self.assertEqual(mailbox.writes, [])
        self.assertEqual(before, self.snapshot())

    def test_partial_failure_rolls_back_all_touched_values(self):
        self.config.write_text(CONFIG + "CORE_OFFSET=150\nCACHE_OFFSET=150\n")
        mailbox = FakeMailbox({0: cpl.encode_offset("20"), 2: cpl.encode_offset("30")})
        mailbox.fail = 2
        before = self.snapshot()
        initial = dict(mailbox.values)
        with self.assertRaisesRegex(cpl.Error, "injected"):
            self.run_cli(mailbox=mailbox)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(mailbox.values, initial)
        self.assertEqual([d for d, _ in mailbox.writes], [0, 2, 2, 0])

    def test_final_readback_detects_unmanaged_domain_change(self):
        mailbox = FakeMailbox()
        original = mailbox.write
        def write(domain, bits):
            original(domain, bits)
            mailbox.values[3] = cpl.encode_offset("5")
        mailbox.write = write
        self.config.write_text(CONFIG + "CORE_OFFSET=150\n")
        with self.assertRaisesRegex(cpl.Error, "Unmanaged offset changed"):
            self.run_cli(mailbox=mailbox)
        self.assertTrue(all(domain == 0 for domain, _ in mailbox.writes))
        self.assertEqual(mailbox.values[0], 0)
        self.assertEqual(self.limits.read(), (45000000, 90000000))

    def test_interruption_restores_touched_settings(self):
        mailbox = FakeMailbox()
        original = mailbox.write
        interrupted = False
        def write(domain, bits):
            nonlocal interrupted
            original(domain, bits)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
        mailbox.write = write
        self.config.write_text(CONFIG + "CORE_OFFSET=150\n")
        with self.assertRaises(KeyboardInterrupt):
            self.run_cli(mailbox=mailbox)
        self.assertEqual(mailbox.values[0], 0)
        self.assertEqual(self.limits.read(), (45000000, 90000000))

    def test_rollback_refuses_previous_positive_offset(self):
        self.config.write_text(CONFIG + "CORE_OFFSET=150\nCACHE_OFFSET=150\n")
        mailbox = FakeMailbox({0: 10})  # Synthetic positive pre-existing offset.
        mailbox.fail = 2
        errors = io.StringIO()
        with redirect_stderr(errors):
            with self.assertRaises(cpl.Error):
                self.run_cli(mailbox=mailbox)
        self.assertTrue(all(cpl.decode_offset(bits) <= 0 for _, bits in mailbox.writes))
        self.assertEqual(self.limits.read(), (45000000, 90000000))
        # run_cli captures the diagnostic; inspect it with the transaction directly.
        mailbox = FakeMailbox({0: 10})
        mailbox.fail = 0
        with redirect_stderr(errors), self.assertRaises(cpl.Error):
            cpl.apply_settings(self.limits, mailbox, (80000000, 110000000),
                               {0: cpl.encode_offset("150")}, self.limits.read(), dict(mailbox.values))
        self.assertIn("Rollback failed for Core", errors.getvalue())

    def test_mmio_mismatch_attempts_restore(self):
        previous = self.limits.read()
        original = self.limits.write
        calls = []
        def write(target):
            calls.append(target)
            original(target)
            if len(calls) == 1:
                raise cpl.Error("MMIO write/readback mismatch")
        with patch.object(self.limits, "write", side_effect=write), redirect_stderr(io.StringIO()):
            with self.assertRaises(cpl.Error):
                cpl.apply_settings(self.limits, None, (80000000, 110000000), {}, previous, {})
        self.assertEqual(self.limits.read(), previous)
        self.assertEqual(len(calls), 2)


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.mailbox = object.__new__(cpl.Mailbox)
        self.mailbox.fd = 99

    def test_command_read_query_and_apply_encoding(self):
        response = cpl.encode_offset("150") << 21
        with patch.object(cpl.os, "pwrite", return_value=8) as write, patch.object(
                cpl.os, "pread", return_value=struct.pack("<Q", response)):
            self.assertEqual(self.mailbox.read(0), cpl.encode_offset("150"))
            self.mailbox.write(0, cpl.encode_offset("150"))
        requests = [struct.unpack("<Q", call.args[1])[0] for call in write.call_args_list]
        self.assertEqual([(r >> 32) & 0xff for r in requests], [0x10, 0x11, 0x10])
        self.assertEqual((requests[1] >> 21) & 0x7ff, cpl.encode_offset("150"))

    def test_timeout_rejection_short_io_and_mismatch(self):
        for response, message in [((1 << 63), "timeout"), (2 << 32, "rejected")]:
            with self.subTest(message=message), patch.object(cpl.os, "pwrite", return_value=8), patch.object(
                    cpl.os, "pread", return_value=struct.pack("<Q", response)), patch.object(cpl.time, "sleep"):
                with self.assertRaisesRegex(cpl.Error, message):
                    self.mailbox.read(0)
        with patch.object(cpl.os, "pwrite", return_value=4), self.assertRaisesRegex(cpl.Error, "Incomplete"):
            self.mailbox.read(0)
        with patch.object(cpl.os, "pwrite", return_value=8), patch.object(cpl.os, "pread", return_value=b""), self.assertRaises(cpl.Error):
            self.mailbox.read(0)
        with patch.object(self.mailbox, "command", return_value=0), self.assertRaisesRegex(cpl.Error, "mismatch"):
            self.mailbox.write(0, cpl.encode_offset("150"))

    def test_positive_and_analog_writes_never_reach_mailbox(self):
        with patch.object(self.mailbox, "command") as command:
            for domain, bits in [(0, 1), (0, 1023), (4, 0), (0, -1), (0, 2048)]:
                with self.subTest(domain=domain, bits=bits), self.assertRaises(cpl.Error):
                    self.mailbox.write(domain, bits)
            command.assert_not_called()

    def test_status_socket_and_barrier(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "notify")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
                server.bind(path)
                messages = []
                def receive():
                    messages.append(server.recv(4096))
                    data, ancillary, _, _ = server.recvmsg(4096, socket.CMSG_SPACE(4))
                    messages.append(data)
                    for level, kind, raw in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            os.close(struct.unpack("i", raw)[0])
                thread = threading.Thread(target=receive)
                thread.start()
                with patch.dict(os.environ, NOTIFY_SOCKET=path):
                    cpl.notify_status("Last applied; PL1=80 W; CORE_OFFSET=-150.39 mV")
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertTrue(messages[0].startswith(b"STATUS=Last applied"))
                self.assertEqual(messages[1], b"BARRIER=1")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def install(self, *args):
        return subprocess.run(["bash", str(REPO / "install.sh"), "--root", str(self.root), *args],
                              capture_output=True, text=True, timeout=10)

    def test_offline_install_preserves_config_and_permissions(self):
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stderr)
        config = self.root / "etc/cpu-power-limits.conf"
        custom = CONFIG + "CORE_OFFSET=25\n"
        config.write_text(custom)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(config.read_text(), custom)
        for name, mode in (("usr/local/libexec/cpu-power-limits", 0o755),
                           ("usr/lib/systemd/system-sleep/cpu-power-limits", 0o755),
                           ("etc/cpu-power-limits.conf", 0o644),
                           ("etc/systemd/system/cpu-power-limits.service", 0o644),
                           ("etc/udev/rules.d/91-cpu-power-limits.rules", 0o644)):
            self.assertEqual((self.root / name).stat().st_mode & 0o777, mode)
        link = self.root / "usr/local/bin/cpu-power-limits"
        self.assertTrue(link.is_symlink())
        result = subprocess.run([str(link), "--config", str(config), "--validate"], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_invalid_config_and_offline_live_flags_never_install(self):
        (self.root / "etc").mkdir()
        (self.root / "etc/cpu-power-limits.conf").write_text("invalid")
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse((self.root / "usr").exists())
        self.assertNotEqual(self.install("--disable-thermald").returncode, 0)

    def test_thermald_choices_noninteractive_and_failures(self):
        # Source functions and replace systemctl: no host services are contacted.
        harness = r'''
source "$1/install.sh"
systemctl() {
    printf '%s\n' "$*" >> "$TEST_LOG"
    case "$1" in
        cat) [[ "${ABSENT:-0}" != 1 ]] ;;
        is-active) [[ "${STILL_ACTIVE:-0}" == 1 ]] ;;
        is-enabled) [[ "${MASK_FAIL:-0}" == 1 ]] || echo masked ;;
        mask) [[ "${MASK_FAIL:-0}" != 1 ]] ;;
        *) return 0 ;;
    esac
}
configure_thermald "$2"
'''
        log = self.root / "commands"
        for policy, variables, expected in (("keep", {}, 0), ("ask", {}, 0), ("disable", {}, 0),
                                             ("disable", {"STILL_ACTIVE": "1"}, 1),
                                             ("disable", {"MASK_FAIL": "1"}, 1),
                                             ("disable", {"ABSENT": "1", "MASK_FAIL": "1"}, 0)):
            log.write_text("")
            env = dict(os.environ, TEST_LOG=str(log), **variables)
            result = subprocess.run(["bash", "-c", harness, "test", str(REPO), policy],
                                    env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            commands = log.read_text()
            if policy in ("keep", "ask") or variables.get("ABSENT"):
                self.assertNotIn("disable --now", commands)
            elif expected == 0:
                self.assertIn("disable --now thermald.service", commands)
                self.assertIn("mask thermald.service dbus-org.freedesktop.thermald.service", commands)

    def test_interactive_thermald_prompt_yes_no_and_default(self):
        for answer, should_disable in ((b"y\n", True), (b"n\n", False), (b"\n", False)):
            harness = r'''
source "$1/install.sh"
systemctl() {
    case "$1" in
        cat) return 0 ;;
        is-active) return 1 ;;
        is-enabled) echo masked ;;
        disable) echo DISABLED ;;
    esac
}
configure_thermald ask
'''
            master, slave = pty.openpty()
            try:
                proc = subprocess.Popen(["bash", "-c", harness, "test", str(REPO)], stdin=slave,
                                        stdout=slave, stderr=slave)
                os.close(slave)
                slave = None
                output = b""
                while b"[y/N]" not in output:
                    self.assertTrue(select.select([master], [], [], 3)[0])
                    output += os.read(master, 4096)
                os.write(master, answer)
                proc.wait(timeout=3)
                while select.select([master], [], [], 0.1)[0]:
                    try:
                        output += os.read(master, 4096)
                    except OSError:
                        break
                self.assertEqual(proc.returncode, 0, output)
                self.assertEqual(b"DISABLED" in output, should_disable)
            finally:
                os.close(master)
                if slave is not None:
                    os.close(slave)

    def test_legacy_unit_migration_uses_only_known_paths(self):
        harness = r'''
source "$1/install.sh"
systemctl() { printf 'systemctl %s\n' "$*"; }
rm() { printf 'rm %s\n' "$*"; }
migrate_legacy_units
'''
        result = subprocess.run(["bash", "-c", harness, "test", str(REPO)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        for unit in ("cpu-power-limits@ac.service", "cpu-power-limits@battery.service", "cpu-power-limits-resume.service"):
            self.assertIn("disable --now " + unit, result.stdout)
        self.assertNotIn("power-state.service", result.stdout)
        self.assertNotIn("cpu-power-limits.conf", result.stdout)

    def test_sleep_hook_only_restarts_own_service_after_suspend(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "systemctl").write_text('#!/bin/bash\nprintf "%s\\n" "$*"\n')
        (bindir / "systemctl").chmod(0o755)
        for phase, kind in (("pre", "suspend"), ("post", "hibernate"), ("post", "suspend")):
            result = subprocess.run(["bash", str(REPO / "cpu-power-limits-sleep"), phase, kind],
                                    env=dict(os.environ, PATH=str(bindir) + ":" + os.environ["PATH"]),
                                    capture_output=True, text=True)
            expected = "--no-block restart cpu-power-limits.service\n" if (phase, kind) == ("post", "suspend") else ""
            self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
