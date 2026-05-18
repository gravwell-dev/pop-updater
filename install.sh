#!/usr/bin/env bash
# Install (or uninstall) Pop Updater for the current user.
# Run as your normal user — it will prompt for sudo when it needs root.

set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
    echo "Error: run this as your normal user, not root." >&2
    echo "       (The script will prompt for sudo when it needs root.)" >&2
    exit 1
fi

USER_NAME="$(id -un)"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

INSTALL_DIR="${HOME}/pop-updater"
LAUNCHER="${HOME}/.local/bin/pop-updater"
DESKTOP_FILE="${HOME}/.local/share/applications/pop-updater.desktop"
TRAY_UNIT="${HOME}/.config/systemd/user/pop-updater-tray.service"
SUDOERS_FILE="/etc/sudoers.d/pop-updater-apt"
CONFIG_FILE="${HOME}/.config/pop-updater.json"
CACHE_DIR="${HOME}/.cache/pop-updater"

uninstall() {
    echo "==> Stopping and disabling tray service"
    systemctl --user disable --now pop-updater-tray.service 2>/dev/null || true

    echo "==> Removing user files"
    rm -f "${LAUNCHER}" "${DESKTOP_FILE}" "${TRAY_UNIT}" "${CONFIG_FILE}"
    rm -rf "${CACHE_DIR}"

    echo "==> Removing sudoers rule (requires sudo)"
    sudo rm -f "${SUDOERS_FILE}"

    systemctl --user daemon-reload

    echo
    echo "✓ Pop Updater uninstalled."
    echo "  Source tree at ${INSTALL_DIR} was left in place — remove it"
    echo "  yourself if you don't want it."
}

case "${1:-install}" in
    --uninstall|uninstall)
        uninstall
        exit 0
        ;;
    install|"")
        ;;
    *)
        echo "Usage: $0 [install|--uninstall]" >&2
        exit 2
        ;;
esac

echo "==> Installing apt dependencies"
MISSING=()
for pkg in python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-ayatanaappindicator3-0.1; do
    if ! dpkg -s "${pkg}" >/dev/null 2>&1; then
        MISSING+=("${pkg}")
    fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "    Need: ${MISSING[*]}"
    sudo apt-get update
    sudo apt-get install -y "${MISSING[@]}"
else
    echo "    All dependencies already present."
fi

echo "==> Creating directories"
mkdir -p "${INSTALL_DIR}" \
         "$(dirname "${LAUNCHER}")" \
         "$(dirname "${DESKTOP_FILE}")" \
         "$(dirname "${TRAY_UNIT}")"

echo "==> Installing pop_updater.py to ${INSTALL_DIR}"
if [[ "$(realpath "${SCRIPT_DIR}/pop_updater.py")" != \
      "$(realpath "${INSTALL_DIR}/pop_updater.py" 2>/dev/null || echo /no/such)" ]]; then
    install -m 0755 "${SCRIPT_DIR}/pop_updater.py" "${INSTALL_DIR}/pop_updater.py"
else
    echo "    Source already at install location — skipping copy."
fi

echo "==> Installing launcher at ${LAUNCHER}"
cat > "${LAUNCHER}" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 "$HOME/pop-updater/pop_updater.py" "$@"
EOF
chmod +x "${LAUNCHER}"

echo "==> Installing desktop entry"
cat > "${DESKTOP_FILE}" <<EOF
[Desktop Entry]
Type=Application
Name=Pop Updater
Comment=Check and install updates from all your package managers
Exec=pop-updater
Icon=system-software-update
Terminal=false
Categories=System;Settings;PackageManager;
StartupNotify=true
EOF

echo "==> Installing systemd user unit"
cat > "${TRAY_UNIT}" <<'EOF'
[Unit]
Description=Pop Updater tray icon
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart=%h/.local/bin/pop-updater --tray
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF

echo "==> Installing sudoers rule (passwordless apt-get update, ${USER_NAME})"
TMP_SUDO="$(mktemp)"
cat > "${TMP_SUDO}" <<EOF
# Installed by pop-updater so it can refresh apt indexes without prompting.
# Restricted to 'apt-get update' only — installs still require auth.
${USER_NAME} ALL=(root) NOPASSWD: /usr/bin/apt-get update, /usr/bin/apt-get update -qq
EOF
sudo visudo -cf "${TMP_SUDO}" >/dev/null
sudo install -m 0440 -o root -g root "${TMP_SUDO}" "${SUDOERS_FILE}"
rm -f "${TMP_SUDO}"

echo "==> Enabling tray service"
systemctl --user daemon-reload
systemctl --user enable --now pop-updater-tray.service

echo
echo "✓ Pop Updater installed."
echo "  GUI:     'pop-updater' (or look for it in your app launcher)"
echo "  Tray:    look for the update icon in your panel's status area"
echo "  Update:  re-run this script after 'git pull' to refresh installed files"
echo "  Remove:  $0 --uninstall"
