#!/usr/bin/env python3
"""Intel MMIO package power limits and optional OC-mailbox undervolt offsets."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import fcntl
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import time


POWER_KEYS = ("AC_PL1", "AC_PL2", "BATTERY_PL1", "BATTERY_PL2")
DOMAINS = {"CORE_OFFSET": 0, "CACHE_OFFSET": 2, "IGPU_OFFSET": 1, "UNCORE_OFFSET": 3}
NAMES = {0: "Core", 1: "iGPU", 2: "Cache/ring", 3: "System Agent (uncore)"}
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")


class Error(RuntimeError):
    """An actionable configuration, hardware, or verification failure."""


def encode_offset(value):
    """Normalize to undervolt and quantize to signed 11-bit units of 1/1024 V."""
    value = Decimal(value)
    if not value.is_finite() or abs(value) > 1000:
        raise Error("Offset magnitude must be at most 1000 mV")
    units = int((-abs(value) * Decimal("1.024")).to_integral_value(rounding=ROUND_HALF_EVEN))
    return units & 0x7ff


def decode_offset(bits):
    if not isinstance(bits, int) or not 0 <= bits <= 0x7ff:
        raise Error("Invalid offset encoding")
    return Decimal(bits - 0x800 if bits & 0x400 else bits) / Decimal("1.024")


def read_config(path):
    settings = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in (*POWER_KEYS, *DOMAINS):
            raise Error(f"Invalid configuration at line {number}")
        if key in settings:
            raise Error(f"Duplicate setting: {key}")
        if len(value) > 64:
            raise Error(f"Value too long: {key}")
        if key in POWER_KEYS:
            if not re.fullmatch(r"[1-9][0-9]*", value) or int(value) > 9223372036854:
                raise Error(f"{key} must be a positive integer in watts within the supported range")
            settings[key] = int(value) * 1000000
        else:
            if not NUMBER.fullmatch(value):
                raise Error(f"{key} must be a decimal number in mV")
            encode_offset(value)
            settings[key] = value
    for key in POWER_KEYS:
        if key not in settings:
            raise Error(f"Missing setting: {key}")
    for prefix in ("AC", "BATTERY"):
        if settings[f"{prefix}_PL1"] > settings[f"{prefix}_PL2"]:
            raise Error(f"{prefix} PL1 must not exceed PL2")
    return settings


def detect_profile(root):
    seen = False
    for supply in sorted(Path(root).glob("*")):
        if not (supply / "type").is_file():
            continue
        if (supply / "type").read_text().strip() not in ("Mains", "USB", "USB_C", "USB_PD", "USB_PD_DRP"):
            continue
        if (supply / "scope").exists() and (supply / "scope").read_text().strip() == "Device":
            continue
        online = (supply / "online").read_text().strip()
        if online == "1":
            return "ac"
        if online != "0":
            raise Error(f"Invalid supply state: {supply}")
        seen = True
    if not seen:
        raise Error("Cannot determine AC/battery state from power_supply")
    return "battery"


class Limits:
    def __init__(self, root):
        zones = [z for z in Path(root).glob("intel-rapl-mmio:*")
                 if (z / "name").is_file() and (z / "name").read_text().strip() == "package-0"]
        if len(zones) != 1:
            raise Error("Expected exactly one Intel RAPL MMIO package-0")
        self.files = []
        for name in ("long_term", "short_term"):
            matches = [p for p in zones[0].glob("constraint_*_name") if p.read_text().strip() == name]
            if len(matches) != 1:
                raise Error(f"Missing or ambiguous MMIO {name} constraint")
            self.files.append(matches[0].with_name(matches[0].name.removesuffix("_name") + "_power_limit_uw"))

    def read(self):
        values = []
        for path in self.files:
            value = path.read_text().strip()
            if not re.fullmatch(r"[0-9]{1,19}", value) or int(value) > 9223372036854775807:
                raise Error(f"Invalid MMIO limit: {path}")
            values.append(int(value))
        return tuple(values)

    def writable(self):
        if not all(os.access(p, os.W_OK) for p in self.files):
            raise Error("MMIO limits are not writable (run as root)")

    def write(self, target):
        current = self.read()
        order = (1, 0) if target[0] > current[1] else (0, 1)
        for index in order:
            if target[index] != current[index]:
                self.files[index].write_text(f"{target[index]}\n")
        if self.read() != tuple(target):
            raise Error("MMIO write/readback mismatch")


class Mailbox:
    def __init__(self, path):
        # OC mailbox queries require a command write, even when offsets are unchanged.
        self.fd = os.open(path, os.O_RDWR)

    def close(self):
        os.close(self.fd)

    def command(self, command, domain, data=0):
        request = (1 << 63) | (domain << 40) | (command << 32) | data
        if os.pwrite(self.fd, struct.pack("<Q", request), 0x150) != 8:
            raise Error("Incomplete OC mailbox write")
        for _ in range(100):
            raw = os.pread(self.fd, 8, 0x150)
            if len(raw) != 8:
                raise Error("Incomplete OC mailbox read")
            response, = struct.unpack("<Q", raw)
            if not response & (1 << 63):
                status = (response >> 32) & 0xff
                if status:
                    raise Error(f"OC mailbox rejected domain {domain}: status {status:#x}")
                return response & 0xffffffff
            time.sleep(0.001)
        raise Error("OC mailbox timeout; hardware state is unconfirmed")

    def read(self, domain):
        return (self.command(0x10, domain) >> 21) & 0x7ff

    def write(self, domain, bits):
        if domain not in DOMAINS.values() or decode_offset(bits) > 0:
            raise Error("Refusing a positive offset or unmanaged domain")
        self.command(0x11, domain, bits << 21)
        if self.read(domain) != bits:
            raise Error(f"{NAMES[domain]} write/readback mismatch")


def offsets_snapshot(mailbox):
    values, errors = {}, {}
    for domain in DOMAINS.values():
        try:
            values[domain] = mailbox.read(domain)
        except (Error, OSError) as error:
            errors[domain] = str(error)
    return values, errors


def apply_settings(limits, mailbox, target, offsets, previous, before):
    touched = []
    power_touched = False
    try:
        # All configured domains must have been read before the first mutation.
        for domain in offsets:
            if domain not in before:
                raise Error(f"Cannot read configured domain: {NAMES[domain]}")
        power_touched = True
        limits.write(target)
        for domain, bits in offsets.items():
            if before[domain] != bits:
                touched.append(domain)  # Include commands that may fail after writing.
                mailbox.write(domain, bits)
        after, errors = offsets_snapshot(mailbox) if mailbox else ({}, {})
        for domain, bits in offsets.items():
            if after.get(domain) != bits:
                raise Error(f"Final offset verification failed: {NAMES[domain]}: {errors.get(domain, 'mismatch')}")
        for domain in before.keys() - offsets.keys():
            if after.get(domain) != before[domain]:
                raise Error(f"Unmanaged offset changed: {NAMES[domain]}")
        if limits.read() != tuple(target):
            raise Error("Final MMIO verification failed")
        return after, errors
    except BaseException:
        print("cpu-power-limits: application failed; attempting rollback", file=sys.stderr)
        for domain in reversed(touched):
            try:
                mailbox.write(domain, before[domain])
            except (Error, OSError) as error:
                print(f"Rollback failed for {NAMES[domain]}: {error}", file=sys.stderr)
        if power_touched:
            try:
                limits.write(previous)
            except (Error, OSError) as error:
                print(f"MMIO rollback failed: {error}", file=sys.stderr)
        raise


@contextmanager
def hardware_lock(path):
    with open(path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def report(settings, actual, offsets, errors, profile=None):
    prefix = "BATTERY" if profile == "battery" else "AC"
    expected = tuple(settings[f"{prefix}_PL{i}"] for i in (1, 2)) if profile else None
    lines = [f"profile={profile or 'not selected'} MMIO PL1={actual[0] / 1e6:g} W PL2={actual[1] / 1e6:g} W"]
    failed = expected is not None and actual != expected
    if expected:
        lines.append(f"Expected PL1={expected[0] / 1e6:g} W PL2={expected[1] / 1e6:g} W: {'MISMATCH' if failed else 'OK'}")
    for key, domain in DOMAINS.items():
        configured = key in settings
        requested = encode_offset(settings[key]) if configured else None
        detail = (f"configured={settings[key]} mV normalized={-abs(Decimal(settings[key])):g} mV "
                  f"expected={decode_offset(requested):+.5f} mV") if configured else "unmanaged (preserve)"
        if domain in offsets:
            match = not configured or offsets[domain] == requested
            detail += f" current={decode_offset(offsets[domain]):+.5f} mV: {'OK' if match else 'MISMATCH'}"
            failed |= not match
        else:
            detail += f" current=unavailable ({errors.get(domain, 'MSR unavailable')})"
            failed |= configured
        lines.append(f"{key} [{NAMES[domain]}]: {detail}")
    return lines, failed


def notify_status(text):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(f"STATUS={text}".encode())
        # Barrier makes the short-lived oneshot wait for systemd to process STATUS.
        read_fd, write_fd = os.pipe()
        try:
            sock.sendmsg([b"BARRIER=1"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", write_fd))])
            os.close(write_fd)
            write_fd = None
            import select
            if not select.select([read_fd], [], [], 2)[0]:
                raise Error("systemd status notification barrier timed out")
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/cpu-power-limits.conf")
    parser.add_argument("--profile", choices=("auto", "ac", "battery"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--status", action="store_true", help="Query current values without setting limits or offsets")
    modes.add_argument("--validate", action="store_true", help="Validate configuration only; no hardware access")
    modes.add_argument("--preflight", action="store_true", help="Validate configuration and hardware before installation")
    args = parser.parse_args(argv)
    settings = read_config(args.config)
    if args.validate:
        print("cpu-power-limits: configuration valid")
        return 0
    root = os.environ.get("CPU_POWER_LIMITS_POWERCAP_ROOT", "/sys/class/powercap")
    supply = os.environ.get("CPU_POWER_LIMITS_POWER_SUPPLY_ROOT", "/sys/class/power_supply")
    lock = os.environ.get("CPU_POWER_LIMITS_LOCK_FILE", "/run/lock/cpu-power-limits.lock")
    msr = os.environ.get("CPU_POWER_LIMITS_MSR_DEVICE", "/dev/cpu/0/msr")
    configured = {domain: encode_offset(settings[key]) for key, domain in DOMAINS.items() if key in settings}
    with hardware_lock(lock):
        profile = args.profile or (None if args.status else "auto")
        if profile == "auto":
            profile = detect_profile(supply)
        limits = Limits(root)
        previous = limits.read()
        if not args.status:
            limits.writable()
        mailbox = None
        try:
            # Only application/preflight with configured undervolt loads the driver.
            if configured and not args.status and not Path(msr).exists() and msr == "/dev/cpu/0/msr":
                subprocess.run(["modprobe", "msr"], check=True, timeout=10)
            try:
                mailbox = Mailbox(msr)
                before, errors = offsets_snapshot(mailbox)
            except (Error, OSError) as error:
                before, errors = {}, dict.fromkeys(DOMAINS.values(), str(error))
            if args.preflight:
                for domain in configured:
                    if domain not in before:
                        raise Error(f"{NAMES[domain]} unavailable: {errors.get(domain)}")
                print("cpu-power-limits: preflight passed (write acceptance is verified during application)")
                return 0
            if not args.status:
                prefix = "BATTERY" if profile == "battery" else "AC"
                target = tuple(settings[f"{prefix}_PL{i}"] for i in (1, 2))
                before, apply_errors = apply_settings(limits, mailbox, target, configured, previous, before)
                errors.update(apply_errors)
            lines, failed = report(settings, limits.read(), before, errors, profile)
            print("\n".join(lines), flush=True)
            if not args.status and not failed:
                timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                summary = f"Last applied {timestamp}; {lines[0]}; " + "; ".join(
                    f"{key}={decode_offset(before[domain]):+.2f} mV" if domain in before else f"{key}=unavailable"
                    for key, domain in DOMAINS.items())
                try:
                    notify_status(summary)
                except (Error, OSError) as error:
                    print(f"cpu-power-limits: warning: status notification failed: {error}", file=sys.stderr)
            return 1 if failed else 0
        finally:
            if mailbox:
                mailbox.close()


def cli():
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        return main()
    except (Error, OSError, InvalidOperation, subprocess.SubprocessError) as error:
        print(f"cpu-power-limits: error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cpu-power-limits: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(cli())
