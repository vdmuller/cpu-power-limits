"""Pure presentation logic, shared by the desktop and headless tests."""

from datetime import datetime
import gettext
import locale
import math

_ = gettext.gettext
BUS = "io.github.vdmuller.CpuPowerLimits"
PATH = "/io/github/vdmuller/CpuPowerLimits"
INTERFACE = BUS + ".Control"
LABELS = {"PL1": "PL1", "PL2": "PL2", "CORE_OFFSET": "Core", "CACHE_OFFSET": "Cache",
          "IGPU_OFFSET": "iGPU", "UNCORE_OFFSET": "Uncore"}


def valid_snapshot(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        return False
    if not isinstance(data.get("sequence"), int) or not isinstance(data.get("checked_at"), str):
        return False
    if data.get("profile") not in (None, "ac", "battery") or data.get("source") not in ("saved", "temporary"):
        return False
    for name in ("readings", "expected"):
        values = data.get(name)
        if not isinstance(values, dict) or any(key not in LABELS or
                (value is not None and (type(value) not in (float, int) or not math.isfinite(value)))
                for key, value in values.items()):
            return False
    for name in ("configuration", "saved_configuration"):
        if not isinstance(data.get(name), dict) or any(not isinstance(value, str) for value in data[name].values()):
            return False
    errors = data.get("errors")
    if not isinstance(errors, list) or any(not isinstance(error, dict) or not isinstance(error.get("detail"), str) for error in errors):
        return False
    last = data.get("last_application")
    return last is None or (isinstance(last, dict) and isinstance(last.get("id"), str)
                            and isinstance(last.get("error"), str) and isinstance(last.get("success"), bool))


def number(value, digits=2):
    if value is None:
        return "—"
    text = locale.format_string(f"%.{digits}f", value, grouping=False)
    if digits:
        text = text.rstrip("0").rstrip(locale.localeconv()["decimal_point"])
    return text.replace("-", "−")


def local_time(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%x %X")
    except (ValueError, TypeError):
        return _("Unknown")


def card(snapshot, key):
    actual = snapshot.get("readings", {}).get(key)
    expected = snapshot.get("expected", {}).get(key)
    managed = expected is not None
    state = "unavailable" if actual is None else "unmanaged" if not managed else "verified" if abs(actual - expected) < 0.000001 else "warning"
    return {"value": actual, "expected": expected, "state": state,
            "show_expected": managed and state in ("warning", "unavailable"),
            "unit": "W" if key in ("PL1", "PL2") else "mV"}


def overall(snapshot, service_error=""):
    if service_error or (snapshot.get("last_application") or {}).get("error"):
        return "error", _("Application failed")
    if not snapshot.get("configuration") or not snapshot.get("profile"):
        return "unavailable", _("Status unavailable")
    states = [card(snapshot, key)["state"] for key in snapshot.get("expected", {})]
    if "unavailable" in states:
        return "unavailable", _("Some readings are unavailable")
    if "warning" in states:
        return "warning", _("Values differ from configuration")
    if snapshot.get("restoration_pending"):
        return "error", _("Restoration is pending")
    return "verified", _("Applied and verified") if snapshot.get("last_application") else _("Values match configuration")


def new_failure(snapshot, previous_id):
    last = snapshot.get("last_application") or {}
    return bool(last.get("id") and last["id"] != previous_id and not last.get("success", True))
