#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: sudo ./uninstall.sh [--purge-config] [--restore-thermald]
       ./uninstall.sh --root DIR [--purge-config]

Remove this project's service, desktop application, helper and integration files.
Configuration and thermald state are preserved unless explicitly requested.
Current hardware limits and offsets are not reset.
--root removes staged files offline without accessing hardware or services.
EOF
}

remove_files() {
    local root=$1 purge=$2 name
    rm -f -- "$root/etc/systemd/system/cpu-power-limits.service" \
        "$root/etc/udev/rules.d/91-cpu-power-limits.rules" \
        "$root/usr/lib/systemd/system-sleep/cpu-power-limits" \
        "$root/usr/local/bin/cpu-power-limits" \
        "$root/usr/local/libexec/cpu-power-limits"
    rm -f -- "$root/usr/local/bin/cpu-power-limits-indicator" \
        "$root/usr/local/libexec/cpu-power-limits-gui" \
        "$root/usr/local/libexec/cpu-power-limits-helper" \
        "$root/usr/local/libexec/cpu_power_limits.py" \
        "$root/usr/local/libexec/cpu_power_limits_control.py" \
        "$root/usr/local/libexec/cpu_power_limits_view.py" \
        "$root/usr/local/libexec/cpu_power_limits_helper.py" \
        "$root/etc/systemd/system/cpu-power-limits-helper.service" \
        "$root/etc/dbus-1/system.d/io.github.vdmuller.CpuPowerLimits.conf" \
        "$root/usr/share/dbus-1/system-services/io.github.vdmuller.CpuPowerLimits.service" \
        "$root/usr/share/polkit-1/actions/io.github.vdmuller.CpuPowerLimits.policy" \
        "$root/usr/local/share/applications/io.github.vdmuller.CpuPowerLimits.Indicator.desktop" \
        "$root/etc/xdg/autostart/cpu-power-limits.desktop" \
        "$root/usr/local/share/locale/"*/LC_MESSAGES/cpu-power-limits.mo \
        "$root/run/cpu-power-limits/status.json" "$root/run/cpu-power-limits/temporary.json"
    for name in cpu-power-limits cpu-power-limits-ok cpu-power-limits-warning cpu-power-limits-error cpu-power-limits-unknown cpu-power-limits-applying; do
        rm -f -- "$root/usr/local/libexec/icons/$name.svg" \
            "$root/usr/local/share/icons/hicolor/scalable/apps/$name.svg"
    done
    for name in cpu_power_limits cpu_power_limits_control cpu_power_limits_view cpu_power_limits_helper; do
        rm -f -- "$root/usr/local/libexec/__pycache__/$name".cpython-*.pyc
    done
    rmdir -- "$root/run/cpu-power-limits" 2>/dev/null || true
    if [[ "$purge" == true ]]; then
        rm -f -- "$root/etc/cpu-power-limits.conf"
    fi
}

uninstall_live() {
    local purge=$1 restore=$2 state
    # A failed systemd connection must not be mistaken for an absent service.
    state=$(systemctl show --property=LoadState --value cpu-power-limits.service)
    if [[ "$state" != not-found ]]; then
        systemctl disable --now cpu-power-limits.service
    fi
    stop_indicators
    state=$(systemctl show --property=LoadState --value cpu-power-limits-helper.service)
    if [[ "$state" != not-found ]]; then
        systemctl stop cpu-power-limits-helper.service
    fi
    remove_files "" "$purge"
    systemctl daemon-reload
    systemctl reload dbus.service
    udevadm control --reload-rules
    if [[ "$restore" == true ]]; then
        systemctl unmask thermald.service dbus-org.freedesktop.thermald.service
        systemctl enable --now thermald.service
    fi
}

stop_indicators() {
    local runtime uid username
    # Address only this application's exported quit action, in each login bus.
    for runtime in /run/user/[0-9]*; do
        [[ -S "$runtime/bus" ]] || continue
        uid=${runtime##*/}
        username=$(getent passwd "$uid" | cut -d: -f1)
        [[ -n "$username" ]] || continue
        runuser -u "$username" -- env XDG_RUNTIME_DIR="$runtime" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus" \
            /usr/local/bin/cpu-power-limits-indicator --quit || true
    done
}

main() {
    local root="" purge=false restore=false command
    while (( $# )); do
        case "$1" in
            --purge-config) purge=true; shift ;;
            --restore-thermald) restore=true; shift ;;
            --root)
                [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || { usage >&2; return 1; }
                root=$2; shift 2 ;;
            --help|-h) usage; return 0 ;;
            *) usage >&2; return 1 ;;
        esac
    done
    if [[ -n "$root" ]]; then
        [[ -d "$root" ]] || { printf '%s\n' '--root must be an existing directory.' >&2; return 1; }
        root=$(cd "$root" && pwd -P)
        [[ "$root" != / && "$restore" == false ]] || {
            printf '%s\n' '--root must be offline and cannot restore thermald.' >&2; return 1;
        }
        remove_files "$root" "$purge"
        printf '%s\n' 'Staged files removed; no hardware or services accessed.'
        return 0
    fi
    [[ "$EUID" == 0 ]] || { printf '%s\n' 'Run with sudo, or use --root DIR for offline removal.' >&2; return 1; }
    for command in systemctl udevadm flock getent cut runuser; do
        command -v "$command" >/dev/null || { printf 'Missing dependency: %s\n' "$command" >&2; return 1; }
    done
    # Serialize with installation; leave lock files in place for existing holders.
    exec 8>/run/lock/cpu-power-limits-install.lock
    flock -x 8
    uninstall_live "$purge" "$restore"
    printf '%s\n' 'Uninstalled cpu-power-limits. Current hardware limits and offsets were not reset.'
    if [[ "$purge" == false ]]; then
        printf '%s\n' 'Configuration left unchanged: /etc/cpu-power-limits.conf'
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
