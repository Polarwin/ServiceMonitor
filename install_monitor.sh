#!/usr/bin/env bash
# Install the service-monitor systemd service: a root watchdog that
# monitors the services listed in service_monitor.json (or, without that
# file, discovers enabled units running something under /home/justin),
# restarts them when they crash or degrade (file-descriptor exhaustion,
# fatal journal errors), and reports every action to Telegram.
set -euo pipefail

SERVICE_NAME="service-monitor"
OLD_SERVICE_NAME="stockticker-monitor"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Resolve the project directory from the script location so it works when run
# as "bash install_monitor.sh" from the project folder.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Runs as root: needs journalctl on all units, systemctl restart, and /proc
# access to count file descriptors of other users' processes.
cat <<EOF | sudo tee "${SERVICE_FILE}" >/dev/null
[Unit]
Description=Watchdog for justin's project services
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/bin/python ${PROJECT_DIR}/service_monitor.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload

# Migrate from the old unit name: stop/disable and remove it if present.
if systemctl cat "${OLD_SERVICE_NAME}.service" >/dev/null 2>&1; then
    echo "Removing old ${OLD_SERVICE_NAME} unit..."
    sudo systemctl disable --now "${OLD_SERVICE_NAME}" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/${OLD_SERVICE_NAME}.service"
    sudo systemctl daemon-reload
fi

# Idempotent start/restart: enable & start if not running, otherwise restart
# so the updated unit file takes effect.
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    echo "${SERVICE_NAME} is already running; restarting with updated unit..."
    sudo systemctl restart "${SERVICE_NAME}"
else
    sudo systemctl enable --now "${SERVICE_NAME}"
fi

echo ""
echo "Service status:"
sudo systemctl status --no-pager "${SERVICE_NAME}"

echo ""
echo "Cheat sheet:"
echo "  Restart:  sudo systemctl restart ${SERVICE_NAME}"
echo "  Stop:     sudo systemctl stop ${SERVICE_NAME}"
echo "  Logs:     sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Status:   sudo systemctl status ${SERVICE_NAME}"
echo "  Disable:  sudo systemctl disable --now ${SERVICE_NAME}"
echo "  Dry run:  ${PROJECT_DIR}/bin/python ${PROJECT_DIR}/service_monitor.py --dry-run"
