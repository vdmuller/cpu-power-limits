#!/bin/bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [--disable-thermald | --keep-thermald]
       ./install.sh --root DIR

Existing /etc/cpu-power-limits.conf is always preserved.
--root stages files offline; it never accesses hardware or starts services.
EOF
}

configure_thermald() {
    local policy=$1 answer="" unit
    if ! systemctl cat thermald.service >/dev/null 2>&1 &&
        [[ "$(systemctl is-enabled thermald.service 2>/dev/null || true)" != masked ]]; then
        printf '%s\n' 'thermald is not installed; skipping.'
        return 0
    fi
    if [[ "$policy" == ask ]]; then
        if [[ -t 0 ]]; then
            printf '%s\n' 'thermald can overwrite power limits; disabling it also removes its thermal management policy.'
            read -r -p 'Stop, disable and persistently mask thermald? [y/N] ' answer || true
            case "${answer,,}" in y|yes) policy=disable ;; *) policy=keep ;; esac
        else
            policy=keep
        fi
    fi
    if [[ "$policy" != disable ]]; then
        printf '%s\n' 'Preserving thermald state. It may overwrite the selected power limits.'
        return 0
    fi
    systemctl disable --now thermald.service || return 1
    systemctl mask thermald.service dbus-org.freedesktop.thermald.service || return 1
    if systemctl is-active --quiet thermald.service; then
        printf '%s\n' 'ERROR: thermald is still active.' >&2
        return 1
    fi
    for unit in thermald.service dbus-org.freedesktop.thermald.service; do
        if [[ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" != masked ]]; then
            printf 'ERROR: %s was not persistently masked.\n' "$unit" >&2
            return 1
        fi
    done
}

migrate_legacy_units() {
    local unit
    for unit in cpu-power-limits@ac.service cpu-power-limits@battery.service cpu-power-limits-resume.service; do
        if systemctl cat "$unit" >/dev/null 2>&1; then
            systemctl disable --now "$unit" || return 1
        fi
    done
    rm -f /etc/systemd/system/cpu-power-limits@.service \
        /etc/systemd/system/cpu-power-limits-resume.service \
        /usr/local/libexec/cpu-power-limits-resume
}

install_files() {
    local root=$1 source language
    install -Dm755 "$SOURCE_DIR/cpu_power_limits.py" "$root/usr/local/libexec/cpu-power-limits"
    for source in cpu_power_limits.py cpu_power_limits_control.py cpu_power_limits_view.py cpu_power_limits_helper.py; do
        install -Dm644 "$SOURCE_DIR/$source" "$root/usr/local/libexec/$source"
    done
    install -Dm755 "$SOURCE_DIR/cpu_power_limits_gui.py" "$root/usr/local/libexec/cpu-power-limits-gui"
    install -Dm755 "$SOURCE_DIR/cpu_power_limits_helper.py" "$root/usr/local/libexec/cpu-power-limits-helper"
    install -d -m755 "$root/usr/local/bin"
    ln -sfn ../libexec/cpu-power-limits "$root/usr/local/bin/cpu-power-limits"
    ln -sfn ../libexec/cpu-power-limits-gui "$root/usr/local/bin/cpu-power-limits-indicator"
    for source in "$SOURCE_DIR"/icons/*.svg; do
        install -Dm644 "$source" "$root/usr/local/libexec/icons/${source##*/}"
        install -Dm644 "$source" "$root/usr/local/share/icons/hicolor/scalable/apps/${source##*/}"
    done
    install -d -m755 "$root/usr/local/share/applications" "$root/etc/xdg/autostart"
    if compgen -G "$SOURCE_DIR/po/*.po" >/dev/null; then
        msgfmt --desktop --template="$SOURCE_DIR/data/cpu-power-limits.desktop.in" \
            -d "$SOURCE_DIR/po" --output-file="$root/usr/local/share/applications/io.github.vdmuller.CpuPowerLimits.Indicator.desktop"
        msgfmt --desktop --template="$SOURCE_DIR/data/cpu-power-limits-autostart.desktop.in" \
            -d "$SOURCE_DIR/po" --output-file="$root/etc/xdg/autostart/cpu-power-limits.desktop"
    else
        install -m644 "$SOURCE_DIR/data/cpu-power-limits.desktop.in" "$root/usr/local/share/applications/io.github.vdmuller.CpuPowerLimits.Indicator.desktop"
        install -m644 "$SOURCE_DIR/data/cpu-power-limits-autostart.desktop.in" "$root/etc/xdg/autostart/cpu-power-limits.desktop"
    fi
    chmod 644 "$root/usr/local/share/applications/io.github.vdmuller.CpuPowerLimits.Indicator.desktop" "$root/etc/xdg/autostart/cpu-power-limits.desktop"
    for source in "$SOURCE_DIR"/po/*.po; do
        [[ -e "$source" ]] || continue
        language=${source##*/}; language=${language%.po}
        [[ "$language" =~ ^[a-zA-Z_@.-]+$ ]] || { printf 'Invalid locale: %s\n' "$language" >&2; return 1; }
        install -d -m755 "$root/usr/local/share/locale/$language/LC_MESSAGES"
        msgfmt --check --output-file="$root/usr/local/share/locale/$language/LC_MESSAGES/cpu-power-limits.mo" "$source"
        chmod 644 "$root/usr/local/share/locale/$language/LC_MESSAGES/cpu-power-limits.mo"
    done
    install -Dm644 "$SOURCE_DIR/cpu-power-limits-helper.service" "$root/etc/systemd/system/cpu-power-limits-helper.service"
    install -Dm644 "$SOURCE_DIR/data/io.github.vdmuller.CpuPowerLimits.conf" "$root/etc/dbus-1/system.d/io.github.vdmuller.CpuPowerLimits.conf"
    install -Dm644 "$SOURCE_DIR/data/io.github.vdmuller.CpuPowerLimits.service" "$root/usr/share/dbus-1/system-services/io.github.vdmuller.CpuPowerLimits.service"
    install -Dm644 "$SOURCE_DIR/data/io.github.vdmuller.CpuPowerLimits.policy" "$root/usr/share/polkit-1/actions/io.github.vdmuller.CpuPowerLimits.policy"
    install -Dm755 "$SOURCE_DIR/cpu-power-limits-sleep" "$root/usr/lib/systemd/system-sleep/cpu-power-limits"
    if [[ ! -e "$root/etc/cpu-power-limits.conf" && ! -L "$root/etc/cpu-power-limits.conf" ]]; then
        install -Dm644 "$SOURCE_DIR/cpu-power-limits.conf" "$root/etc/cpu-power-limits.conf"
    else
        printf '%s\n' 'Preserving existing cpu-power-limits.conf.'
    fi
    install -Dm644 "$SOURCE_DIR/cpu-power-limits.service" "$root/etc/systemd/system/cpu-power-limits.service"
    install -Dm644 "$SOURCE_DIR/91-cpu-power-limits.rules" "$root/etc/udev/rules.d/91-cpu-power-limits.rules"
}

main() {
    local root="" policy=ask command config catalog
    while (( $# )); do
        case "$1" in
            --disable-thermald|--keep-thermald)
                [[ "$policy" == ask ]] || { printf '%s\n' 'Choose one thermald option.' >&2; return 1; }
                if [[ "$1" == --disable-thermald ]]; then policy=disable; else policy=keep; fi
                shift ;;
            --root)
                [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || { usage >&2; return 1; }
                root=$2; shift 2 ;;
            --help|-h) usage; return 0 ;;
            *) usage >&2; return 1 ;;
        esac
    done
    for command in python3 install ln msgfmt; do
        command -v "$command" >/dev/null || { printf 'Missing dependency: %s\n' "$command" >&2; return 1; }
    done
    for catalog in "$SOURCE_DIR"/po/*.po; do
        [[ -e "$catalog" ]] || continue
        msgfmt --check --output-file=/dev/null "$catalog"
    done
    if [[ -n "$root" ]]; then
        [[ -d "$root" ]] || { printf '%s\n' '--root must be an existing directory.' >&2; return 1; }
        root=$(cd "$root" && pwd -P)
        [[ "$root" != / && "$policy" == ask ]] || { printf '%s\n' '--root must be offline and cannot change thermald.' >&2; return 1; }
        config="$root/etc/cpu-power-limits.conf"
        [[ -e "$config" || -L "$config" ]] || config="$SOURCE_DIR/cpu-power-limits.conf"
        python3 "$SOURCE_DIR/cpu_power_limits.py" --config "$config" --validate
        install_files "$root"
        printf '%s\n' 'Offline files staged; no hardware or services accessed.'
        return 0
    fi
    [[ "$EUID" == 0 ]] || { printf '%s\n' 'Run with sudo, or use --root DIR for offline staging.' >&2; return 1; }
    /usr/bin/python3 - <<'PY'
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    gi.require_version('Dbusmenu', '0.4')
    from gi.repository import Gtk, Adw, Gio, Dbusmenu
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 5):
        raise ValueError("libadwaita 1.5 or newer is required")
except (ImportError, ValueError) as error:
    raise SystemExit('Missing desktop dependencies: install PyGObject, GTK 4, libadwaita 1.5+ and Dbusmenu. ' + str(error))
PY
    for command in systemctl udevadm flock modprobe; do
        command -v "$command" >/dev/null || { printf 'Missing dependency: %s\n' "$command" >&2; return 1; }
    done
    # Separate installer processes cannot race configuration preservation or migrations.
    exec 8>/run/lock/cpu-power-limits-install.lock
    flock -x 8
    config=/etc/cpu-power-limits.conf
    [[ -e "$config" || -L "$config" ]] || config="$SOURCE_DIR/cpu-power-limits.conf"
    python3 "$SOURCE_DIR/cpu_power_limits.py" --config "$config" --preflight
    configure_thermald "$policy"
    migrate_legacy_units
    install_files ""
    systemctl daemon-reload
    # D-Bus reloads policy; restarting its daemon would disrupt the desktop.
    systemctl reload dbus.service
    systemctl try-restart cpu-power-limits-helper.service
    udevadm control --reload-rules
    systemctl enable cpu-power-limits.service
    systemctl restart cpu-power-limits.service
    /usr/local/libexec/cpu-power-limits --managed --status --profile auto
    printf '%s\n' 'Installed and verified cpu-power-limits.service.'
    printf '%s\n' 'Desktop indicator installed. Open CPU Power Limits from the applications menu, or log in again for autostart.'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
