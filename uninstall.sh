#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: sudo ./uninstall.sh [--purge-config] [--restore-thermald]
       ./uninstall.sh --root DIR [--purge-config]

Remove this project's service, executable, udev rule and suspend hook.
Configuration and thermald state are preserved unless explicitly requested.
Current hardware limits and offsets are not reset.
--root removes staged files offline without accessing hardware or services.
EOF
}

remove_files() {
    local root=$1 purge=$2
    rm -f -- "$root/etc/systemd/system/cpu-power-limits.service" \
        "$root/etc/udev/rules.d/91-cpu-power-limits.rules" \
        "$root/usr/lib/systemd/system-sleep/cpu-power-limits" \
        "$root/usr/local/bin/cpu-power-limits" \
        "$root/usr/local/libexec/cpu-power-limits"
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
    remove_files "" "$purge"
    systemctl daemon-reload
    udevadm control --reload-rules
    if [[ "$restore" == true ]]; then
        systemctl unmask thermald.service dbus-org.freedesktop.thermald.service
        systemctl enable --now thermald.service
    fi
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
    for command in systemctl udevadm flock; do
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
