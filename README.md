# cpu-power-limits

Apply Intel CPU package power limits for AC and battery operation, with optional
undervolting of Core, Cache/ring, iGPU and System Agent. A single systemd service
applies settings at boot, on power-source changes and after suspend.

Power limits use the kernel's **Intel RAPL MMIO** interface. Voltage offsets use
the Intel OC mailbox at MSR `0x150`. The program verifies every change by reading
it back and attempts to restore touched settings if an application fails.

## Requirements and compatibility

- Linux with systemd, udev and an Intel `intel-rapl-mmio` powercap zone named
  `package-0`, exposing `long_term` and `short_term` constraints.
- Python **3.10+**, Bash, GNU coreutils, `flock` (util-linux) and `modprobe` (kmod).
  No Python packages or third-party undervolting tools are needed.
- Root privileges for installation and hardware operations. Use `sudo` for checks
  too: mailbox queries require read/write access and all queries are serialized.
- For undervolting: the `msr` kernel module, `/dev/cpu/0/msr`, and firmware that
  permits voltage-offset commands. Kernel lockdown or firmware protections can
  prevent access. The installer does not disable those protections.

The original implementation was used on an Intel Core i7-14650HX. Support on
other processors depends on their interfaces and firmware; there is no claim of
universal Intel support. Only one MMIO `package-0` zone is supported; the mailbox
is accessed through CPU 0. AMD and independent multi-socket tuning are unsupported.
Missing MMIO never causes a fallback to MSR power-limit writes.

The program changes only PL1/PL2 power values and explicitly configured voltage
offsets. It does not change PL4, time windows, enable bits, frequencies, thermal
targets or Analog I/O offsets. Other power-limit mechanisms or firmware can still
restrict effective power or overwrite settings later.

## Installation

```bash
git clone git@github.com:vdmuller/cpu-power-limits.git
cd cpu-power-limits
sudo ./install.sh
```

Public HTTPS cloning is also available:

```bash
git clone https://github.com/vdmuller/cpu-power-limits.git
```

The installer preserves an existing `/etc/cpu-power-limits.conf` verbatim. On a
new installation it uses the supplied configuration: AC PL1/PL2 **100/135 W**,
battery PL1/PL2 **35/45 W**, with all undervolt offsets commented out. These power
values are examples from one laptop, not hardware-specific recommendations.
To choose settings **before the first application**:

```bash
sudo install -d -m755 /etc
# Only create the config if it does not already exist:
test -e /etc/cpu-power-limits.conf || sudo install -m644 cpu-power-limits.conf /etc/cpu-power-limits.conf
sudoedit /etc/cpu-power-limits.conf
sudo ./install.sh
```

Installation validates the configuration and hardware, asks about thermald,
updates managed files, reloads systemd/udev, enables and restarts the service,
then checks the active profile. It may load the `msr` module when an offset is
configured. Preflight cannot prove firmware will accept a change; application
performs the actual write/readback verification. Re-running the installer upgrades
the program without replacing the configuration or duplicating rules and hooks.

Installed files:

| Location | Purpose |
| --- | --- |
| `/etc/cpu-power-limits.conf` | User configuration, preserved on reinstall |
| `/usr/local/libexec/cpu-power-limits` | Python executable |
| `/usr/local/bin/cpu-power-limits` | Relative symlink for the CLI |
| `/etc/systemd/system/cpu-power-limits.service` | System service |
| `/etc/udev/rules.d/91-cpu-power-limits.rules` | Power-source change trigger |
| `/usr/lib/systemd/system-sleep/cpu-power-limits` | Resume hook |

For offline packaging or inspection, stage files in an existing directory:

```bash
mkdir -p /tmp/cpu-power-limits-stage
./install.sh --root /tmp/cpu-power-limits-stage
```

Staging validates the configuration and copies files only. It never queries
hardware, loads modules, controls thermald, or enables/restarts services.

## thermald

If thermald is installed, interactive installation asks whether to stop, disable
and persistently mask `thermald.service` and its D-Bus alias. The default is **No**.
This avoids competing power-limit policies when chosen, but also removes thermald's
thermal management policy. The package stays installed.

For unattended installation, choose explicitly:

```bash
sudo ./install.sh --disable-thermald
sudo ./install.sh --keep-thermald
```

Without a terminal or an explicit option, thermald is preserved. Keeping thermald
also preserves an existing mask; it does not automatically re-enable the service.
Other tools can overwrite limits too. If disabling thermald fails, installation
stops before applying this project's settings. Earlier installation steps or
thermald state changes are not automatically undone.

## Configuration

Edit `/etc/cpu-power-limits.conf`. The file is parsed as data, never sourced as
shell code. Blank lines, whitespace and `#` comments are accepted. Duplicate or
unknown keys, missing power keys and invalid values are errors.

```ini
# Positive integer watts; PL1 <= PL2 in each profile.
AC_PL1=100
AC_PL2=135
BATTERY_PL1=35
BATTERY_PL2=45

# Optional global offsets in millivolts. These examples are NOT enabled by default.
# CORE_OFFSET=150
# CACHE_OFFSET=-150
# IGPU_OFFSET=80
# UNCORE_OFFSET=0
```

| Key | Intel domain | Behavior when omitted |
| --- | --- | --- |
| `CORE_OFFSET` | Core, plane 0 | Preserve current offset |
| `CACHE_OFFSET` | Cache/ring, plane 2 | Preserve current offset |
| `IGPU_OFFSET` | Intel integrated GPU, plane 1 | Preserve current offset |
| `UNCORE_OFFSET` | System Agent, plane 3 | Preserve current offset |

Offsets apply in both AC and battery operation. Decimal values are accepted,
including an optional sign. Every request is normalized to **`-abs(value)`** before
quantization: `150`, `+150` and `-150` all request **−150 mV**. This program never
sets a positive voltage offset, including during rollback.

An explicit **`0` resets that domain to zero offset**. Removing or commenting out
a key leaves the hardware's current value in place; it does not reset a previously
applied offset. To remove an undervolt, configure zero and reapply before removing
the key. Stopping the service does not restore previous values.

The maximum accepted magnitude is 1000 mV, reflecting the signed mailbox encoding,
**not a stable or recommended tuning range**. Hardware steps are 1/1024 V, about
0.9765625 mV. Requests round to the nearest step (ties to even):

| Input magnitude | Normalized request | Quantized readback |
| --- | --- | --- |
| 150 mV | −150 mV | −150.390625 mV |
| 80 mV | −80 mV | −80.078125 mV |
| 0 mV | 0 mV | 0 mV |

Validate without hardware access, then reapply:

```bash
cpu-power-limits --config /etc/cpu-power-limits.conf --validate
sudo systemctl restart cpu-power-limits.service
sudo cpu-power-limits --status --profile auto
```

Readback confirms the reported offset, not physical supply voltage or stability.
The example Core/Cache settings passed a CPU screening workload on one processor;
that does not validate another processor or the iGPU. Validate changes gradually
with calculation checking, individual-core and load/idle transitions, graphics
workloads for iGPU changes, battery use and suspend/resume. Instability can cause
crashes or data loss.

## Checking current values

```bash
sudo cpu-power-limits --status
sudo cpu-power-limits --status --profile auto
sudo cpu-power-limits --status --profile ac
sudo cpu-power-limits --status --profile battery
```

Without `--profile`, check reports live PL1/PL2 without comparing them to a power
profile. With a profile, it also compares the limits. Configured offsets are always
compared against their normalized, quantized targets. All results are printed
before returning exit code 1 for a mismatch or unavailable configured offset.
Omitted offsets are reported as unmanaged and may be positive if previously set
by another tool; checking does not change them.

Illustrative output:

```text
profile=ac MMIO PL1=100 W PL2=135 W
Expected PL1=100 W PL2=135 W: OK
CORE_OFFSET [Core]: configured=150 mV normalized=-150 mV expected=-150.39062 mV current=-150.39062 mV: OK
CACHE_OFFSET [Cache/ring]: configured=-150 mV normalized=-150 mV expected=-150.39062 mV current=-150.39062 mV: OK
IGPU_OFFSET [iGPU]: unmanaged (preserve) current=-80.07812 mV: OK
UNCORE_OFFSET [System Agent (uncore)]: unmanaged (preserve) current=+0.00000 mV: OK
```

Unavailable offsets include a reason. With no offsets configured, MSR access is
optional: power limits can be applied and checked successfully on their own.
Checks serialize with applications using `flock`. They do not set limits or
offsets, but querying the OC mailbox necessarily writes a **query command** to
MSR `0x150` before reading its response. Check does not load the MSR module.

Exit codes: 0 for success, 1 for configuration/hardware/verification errors, 2 for
invalid CLI arguments, 130 for interruption. `--help` lists options. Omitting
`--status`, `--validate` and `--preflight` applies settings (automatic profile by
default). `--preflight` validates hardware access without applying targets.

## systemd status and triggers

```bash
systemctl status cpu-power-limits.service
journalctl -u cpu-power-limits.service -b
sudo cpu-power-limits --status --profile auto
```

The service is `oneshot` with `RemainAfterExit=yes`: `active (exited)` is normal.
The systemd `Status` field contains the **last successful application** timestamp
in UTC, profile, PL1/PL2 and read-back offsets. Detailed comparisons are recorded
in the journal. The status is a snapshot, not a live monitor; use `--status` for
current hardware values. Status publication failure is logged as a warning.

Power-supply `change` events request a non-blocking restart. A source of type
Mains, USB, USB_C, USB_PD or USB_PD_DRP with `online=1` selects AC. Sources with
`scope=Device` are ignored. If all recognized sources are offline, battery is
selected. If the source cannot be determined, application fails before changing
settings. Detection occurs after taking the lock so queued events use the current
source. Explicit profiles are available for diagnostics.

The suspend hook requests a restart after resume; the service waits for
`systemd-suspend.service` to finish and detects the source again. It does not
enable hibernation or depend on any external power-management targets/services.
There is no polling or automatic correction between events. Firmware changes
after verification remain until the next trigger or manual restart.

## Failures and recovery

Configuration and readable configured offsets are checked before target writes.
PL2 is raised before PL1 when needed; PL1 is lowered first when reducing limits.
Matching limits and offsets are not rewritten. Touched power limits and managed
offsets are restored on a caught application error or interruption, with readback.
The program reports every rollback failure. An earlier positive offset cannot be
restored because positive offset writes are prohibited; this is explicitly logged.
Omitted domains never receive voltage-setting commands, even during recovery.

Rollback is best effort, not a hardware transaction. Firmware rejection, hardware
failure, abrupt power loss or an uncatchable kill can leave partial settings.
Inspect values with `--status` after errors. A readable mailbox that ignores writes
is detected by a mismatch; blocked mailbox commands report their response status
or timeout. These errors do not cause firmware protections to be bypassed.

## Migration from the linux setup module

Existing four-key configurations remain valid and are preserved. Running this
installer replaces the same main service, executable, udev rule and suspend hook.
It disables the known older `cpu-power-limits@ac.service`,
`cpu-power-limits@battery.service` and `cpu-power-limits-resume.service` units and
removes their template/resume helper. It does not modify `power-state.service`,
AC/battery targets or other projects' udev rules.

The linux setup integration installs a pinned revision of this repository. For
an existing setup completion marker, reset and run the module to migrate:

```bash
./setup.sh --reset cpu-power-limits
./setup.sh --module cpu-power-limits
```

## Removal

Stopping or removing the service leaves currently applied values in hardware.
If desired, first configure zero offsets and your intended power limits, restart
the service and verify. A reboot may restore firmware defaults but is not a
replacement for verifying them.

```bash
sudo ./uninstall.sh
```

The idempotent uninstaller stops and disables the service, removes the installed
files, and reloads systemd and udev. It preserves `/etc/cpu-power-limits.conf` and
thermald's current state by default. Optional flags can be combined:

```bash
sudo ./uninstall.sh --purge-config        # Also remove the configuration.
sudo ./uninstall.sh --restore-thermald    # Unmask, enable and start thermald.
```

Restoring thermald is an explicit choice; its prior state is not recorded.
thermald may adjust hardware limits after it starts. Offline removal is available
with `./uninstall.sh --root /tmp/cpu-power-limits-stage`, optionally with
`--purge-config`. Offline removal never controls services. The source checkout
is retained so it can be used to reinstall later.

## Development and tests

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
bash -n uninstall.sh
bash -n cpu-power-limits-sleep
shellcheck install.sh uninstall.sh cpu-power-limits-sleep
```

Tests use synthetic powercap, power-supply and mailbox fixtures, mocked service
commands and offline install roots. They do not tune the host or run stress tests.
The notification test uses a temporary local Unix socket. CI also validates the
systemd unit against a staged executable.

For isolated fixtures, the CLI supports `CPU_POWER_LIMITS_POWERCAP_ROOT`,
`CPU_POWER_LIMITS_POWER_SUPPLY_ROOT`, `CPU_POWER_LIMITS_LOCK_FILE` and
`CPU_POWER_LIMITS_MSR_DEVICE`. Normal installations should leave these unset.

## Protocol references and license

- [Linux powercap interface](https://docs.kernel.org/power/powercap/powercap.html)
- [Intel OC mailbox implementation in undervolt](https://github.com/georgewhewell/undervolt/blob/master/undervolt.py)
- [systemd status notifications](https://www.freedesktop.org/software/systemd/man/latest/sd_notify.html)

MIT license; see [LICENSE](LICENSE).
