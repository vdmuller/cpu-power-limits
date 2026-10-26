#!/bin/bash
set -euo pipefail
source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$source_dir"
xgettext --language=Python --keyword=_ --from-code=UTF-8 \
    --package-name=cpu-power-limits --package-version=1 --copyright-holder='cpu-power-limits contributors' \
    --output=po/cpu-power-limits.pot cpu_power_limits_gui.py cpu_power_limits_view.py
xgettext --language=Desktop --join-existing --keyword=Name --keyword=Comment --package-name=cpu-power-limits --package-version=1 \
    --output=po/cpu-power-limits.pot data/cpu-power-limits.desktop.in data/cpu-power-limits-autostart.desktop.in
