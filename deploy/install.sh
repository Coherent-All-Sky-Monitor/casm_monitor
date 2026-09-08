#!/usr/bin/env bash
# Symlink the casm_monitor user units into ~/.config/systemd/user/ and reload.
# It deliberately does NOT enable or start anything: enabling is an operator
# decision (linger is already on for this account).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
units_dir="$here/systemd"
target="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

repo_root="$(cd "$here/.." && pwd)"
config_path="$repo_root/config/monitor.yaml"

mkdir -p "$target"
for unit in "$units_dir"/casm-monitor-*.service; do
    name="$(basename "$unit")"
    ln -sfn "$unit" "$target/$name"
    echo "linked $target/$name -> $unit"
    # Drop-in pins CASM_MONITOR_CONFIG to THIS checkout (main repo or a
    # worktree), so the unit file itself stays checkout-agnostic.
    mkdir -p "$target/$name.d"
    printf '[Service]\nEnvironment=CASM_MONITOR_CONFIG=%s\n' "$config_path" > "$target/$name.d/checkout.conf"
    echo "  drop-in $target/$name.d/checkout.conf -> $config_path"
done

systemctl --user daemon-reload
echo
echo "daemon-reload done. Nothing was enabled or started. Next, by hand:"
echo "  systemctl --user enable --now casm-monitor-collect casm-monitor-web casm-monitor-jobs"
echo "  systemctl --user status  casm-monitor-'*'"
