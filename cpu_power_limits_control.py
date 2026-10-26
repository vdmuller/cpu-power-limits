"""Serialized desktop operations. All privileged paths are fixed by the caller."""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import uuid

import cpu_power_limits as core


RUNTIME = "/run/cpu-power-limits"
CONFIG = "/etc/cpu-power-limits.conf"
OPERATIONS = ("save", "test", "end-test", "reapply")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def validate_configuration(data):
    """The wire format contains only known keys and bounded decimal strings."""
    if not isinstance(data, dict) or len(data) > 8:
        raise core.Error("Invalid configuration object")
    allowed = (*core.POWER_KEYS, *core.DOMAINS)
    for key, value in data.items():
        if key not in allowed or not isinstance(value, str) or len(value) > 64:
            raise core.Error("Invalid configuration field")
        if not core.NUMBER.fullmatch(value):
            raise core.Error(f"Invalid numeric value: {key}")
    settings = core.parse_config("\n".join(f"{key}={value}" for key, value in data.items()))
    return settings


def configuration_wire(settings):
    return {key: str(value // 1000000) if key in core.POWER_KEYS else str(abs(Decimal(value)))
            for key, value in settings.items()}


def atomic_write(path, text, mode=0o644):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def merged_configuration(original, data):
    """Replace active managed assignments, retaining comments and blank lines."""
    remaining = dict(data)
    lines = []
    for line in original.splitlines():
        assignment = line.split("#", 1)[0].strip()
        key, separator, _ = assignment.partition("=")
        key = key.strip()
        if separator and key in (*core.POWER_KEYS, *core.DOMAINS):
            if key in remaining:
                comment = " #" + line.split("#", 1)[1] if "#" in line else ""
                lines.append(f"{key}={remaining.pop(key)}{comment}")
            elif "#" in line:
                lines.append("#" + line.split("#", 1)[1])
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(lines) + "\n"


class Controller:
    def __init__(self, config=CONFIG, runtime=RUNTIME, state_file=None, powercap=None,
                 supply=None, msr=None, lock=None):
        self.config = Path(config)
        self.runtime = Path(runtime)
        self.state = Path(state_file) if state_file else self.runtime / "status.json"
        self.override = self.runtime / "temporary.json"
        self.powercap = powercap or os.environ.get("CPU_POWER_LIMITS_POWERCAP_ROOT", "/sys/class/powercap")
        self.supply = supply or os.environ.get("CPU_POWER_LIMITS_POWER_SUPPLY_ROOT", "/sys/class/power_supply")
        self.msr = msr or os.environ.get("CPU_POWER_LIMITS_MSR_DEVICE", "/dev/cpu/0/msr")
        self.lock = lock or os.environ.get("CPU_POWER_LIMITS_LOCK_FILE", "/run/lock/cpu-power-limits.lock")

    @contextmanager
    def transaction(self):
        self.runtime.mkdir(mode=0o755, parents=True, exist_ok=True)
        info = self.runtime.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise core.Error("Runtime directory is not owned exclusively by the service")
        with core.hardware_lock(self.lock):
            yield

    def read_override(self):
        if not self.override.exists():
            return None
        data = json.loads(self.override.read_text())
        # Older releases could leave restored hardware readings as an override.
        # Those states now end by applying the saved configuration.
        if isinstance(data, dict) and (data.get("mode") == "reverting" or data.get("reverted")):
            data["mode"] = "saved"
            data["configuration"] = configuration_wire(core.read_config(self.config))
        if not isinstance(data, dict) or data.get("mode") not in ("temporary", "saved") or not isinstance(data.get("baseline"), dict):
            raise core.Error("Invalid boot-local configuration")
        validate_configuration(data["configuration"])
        for key, bits in data["baseline"].items():
            if key not in core.DOMAINS or core.decode_offset(bits) > 0:
                raise core.Error("Invalid saved offset baseline")
        return data

    def write_override(self, data):
        atomic_write(self.override, json.dumps(data) + "\n")

    def previous_application(self):
        try:
            data = json.loads(self.state.read_text())
            return data.get("last_application") if data.get("version") == 1 else None
        except (OSError, ValueError, AttributeError):
            return None

    def snapshot(self, application=None):
        errors, settings, saved, override = [], {}, {}, None
        try:
            saved = core.read_config(self.config)
        except (core.Error, OSError) as error:
            errors.append({"code": "configuration", "field": "", "detail": str(error)})
        try:
            override = self.read_override()
            settings = validate_configuration(override["configuration"]) if override and override["mode"] == "temporary" else saved
        except (core.Error, OSError, ValueError, KeyError, TypeError) as error:
            errors.append({"code": "temporary", "field": "", "detail": str(error)})
        profile = None
        try:
            profile = core.detect_profile(self.supply)
        except (core.Error, OSError) as error:
            errors.append({"code": "profile", "field": "", "detail": str(error)})
        readings = dict.fromkeys(("PL1", "PL2", *core.DOMAINS))
        try:
            actual = core.Limits(self.powercap).read()
            readings.update(PL1=actual[0] / 1e6, PL2=actual[1] / 1e6)
        except (core.Error, OSError) as error:
            errors.append({"code": "power", "field": "", "detail": str(error)})
        try:
            mailbox = core.Mailbox(self.msr)
        except (core.Error, OSError) as error:
            errors.append({"code": "offset", "field": "", "detail": str(error)})
        else:
            try:
                values, unavailable = core.offsets_snapshot(mailbox)
                for key, domain in core.DOMAINS.items():
                    if domain in values:
                        readings[key] = float(core.decode_offset(values[domain]))
                    if domain in unavailable:
                        errors.append({"code": "offset", "field": key, "detail": unavailable[domain]})
            finally:
                mailbox.close()
        expected = {key: float(core.decode_offset(core.encode_offset(settings[key])))
                    for key in core.DOMAINS if key in settings}
        if profile and settings:
            prefix = "AC" if profile == "ac" else "BATTERY"
            expected.update({f"PL{i}": settings[f"{prefix}_PL{i}"] / 1e6 for i in (1, 2)})
        return {"version": 1, "sequence": time.monotonic_ns(), "checked_at": now(), "profile": profile,
                "source": "temporary" if override and override["mode"] == "temporary" else "saved",
                "restoration_pending": bool(override and override["mode"] == "saved"),
                "configuration": configuration_wire(settings), "saved_configuration": configuration_wire(saved),
                "readings": readings, "expected": expected, "errors": errors,
                "last_application": application if application is not None else self.previous_application()}

    def publish(self, snapshot):
        atomic_write(self.state, json.dumps(snapshot, allow_nan=False) + "\n")
        return snapshot

    def refresh(self):
        with self.transaction():
            return self.publish(self.snapshot())

    def apply_hardware(self, settings, restore, profile):
        selected = core.detect_profile(self.supply) if profile == "auto" else profile
        prefix = "AC" if selected == "ac" else "BATTERY"
        target = tuple(settings[f"{prefix}_PL{i}"] for i in (1, 2))
        configured = {domain: core.encode_offset(settings[key]) for key, domain in core.DOMAINS.items() if key in settings}
        configured.update({core.DOMAINS[key]: bits for key, bits in restore.items() if key not in settings})
        limits = core.Limits(self.powercap)
        limits.writable()
        mailbox = None
        try:
            if configured and not Path(self.msr).exists() and self.msr == "/dev/cpu/0/msr":
                subprocess.run(["modprobe", "msr"], check=True, timeout=10)
            try:
                mailbox = core.Mailbox(self.msr)
                before, _ = core.offsets_snapshot(mailbox)
            except (core.Error, OSError):
                before = {}
            core.apply_settings(limits, mailbox, target, configured, before)
        finally:
            if mailbox:
                mailbox.close()

    def capture_baseline(self, baseline, settings):
        needed = [key for key in core.DOMAINS if key in settings and key not in baseline]
        if not needed:
            return
        if not Path(self.msr).exists() and self.msr == "/dev/cpu/0/msr":
            subprocess.run(["modprobe", "msr"], check=True, timeout=10)
        mailbox = core.Mailbox(self.msr)
        try:
            for key in needed:
                bits = mailbox.read(core.DOMAINS[key])
                if core.decode_offset(bits) > 0:
                    raise core.Error("Cannot safely restore a pre-existing positive offset during a test")
                baseline[key] = bits
        finally:
            mailbox.close()

    def change(self, operation, configuration=None, profile="auto"):
        if operation not in OPERATIONS or profile not in ("auto", "ac", "battery"):
            raise core.Error("Unsupported operation")
        candidate = validate_configuration(configuration) if operation in ("test", "save") else None
        with self.transaction():
            application = {"id": str(uuid.uuid4()), "invocation_id": os.environ.get("INVOCATION_ID", ""),
                           "timestamp": now(), "operation": operation, "success": False,
                           "configuration_saved": False, "error": ""}
            try:
                override = self.read_override()
                baseline = dict(override["baseline"]) if override else {}
                restore = {}
                if operation == "test":
                    self.capture_baseline(baseline, candidate)
                    self.write_override({"mode": "temporary", "configuration": configuration_wire(candidate), "baseline": baseline})
                    settings = candidate
                    restore = baseline
                elif operation in ("save", "end-test"):
                    if operation == "save":
                        original = self.config.read_text() if self.config.exists() else ""
                        mode = stat.S_IMODE(self.config.stat().st_mode) if self.config.exists() else 0o644
                        merged = merged_configuration(original, configuration_wire(candidate))
                        core.parse_config(merged)
                        atomic_write(self.config, merged, mode)
                        application["configuration_saved"] = True
                    settings = candidate if candidate is not None else core.read_config(self.config)
                    restore = baseline
                    if override:
                        self.write_override({"mode": "saved", "configuration": configuration_wire(settings), "baseline": baseline})
                else:
                    settings = validate_configuration(override["configuration"]) if override and override["mode"] == "temporary" else core.read_config(self.config)
                    if override and override["mode"] == "saved":
                        restore = baseline
                selected_profile = core.detect_profile(self.supply) if profile == "auto" else profile
                application["profile"] = selected_profile
                application["configuration"] = configuration_wire(settings)
                self.apply_hardware(settings, restore, selected_profile)
                if self.override.exists() and self.read_override()["mode"] == "saved":
                    self.override.unlink()
                application["success"] = True
            except (core.Error, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                application["error"] = str(error)
            except KeyboardInterrupt:
                application["error"] = "Application interrupted; hardware state may be partial"
                self.publish(self.snapshot(application))
                raise
            snapshot = self.snapshot(application)
            return self.publish(snapshot)
