#!/usr/bin/env bash
# Install Hobby Server Monitor as three systemd services.
#
#   sudo bash deploy/install.sh            (from the repository root, after
#                                           `scripts/setup-dev-host.sh`, a filled .env
#                                           and `make build-ui`)
#
# What it changes, and why:
#   - system users hsm-api, hsm-worker, hsm-collector in a shared group `hsm`
#     (shared SQLite); ONLY worker and collector join `lxd` (root-equivalent).
#   - /opt/hsm         code + venv, owned by root, read-only to the services
#   - /etc/hsm/hsm.env configuration, root:hsm 0640 (contains secrets)
#   - /var/lib/hsm     SQLite (root:hsm 2770); metrics/ is collector-only (0700)
#   - systemd units, enabled at boot.
# It never touches LXD configuration.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)
[[ -f "$SRC/.env" ]] || { echo "missing $SRC/.env (copy .env.example and fill it in)" >&2; exit 1; }
[[ -f "$SRC/dashboard/dist/index.html" ]] || { echo "build the dashboard first: make build-ui" >&2; exit 1; }
getent group lxd >/dev/null || { echo "group lxd missing: install LXD first (scripts/setup-dev-host.sh)" >&2; exit 1; }

echo "==> users and groups"
getent group hsm >/dev/null || groupadd --system hsm
for u in hsm-api hsm-worker hsm-collector; do
  id "$u" >/dev/null 2>&1 || useradd --system --gid hsm --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin "$u"
done
usermod -aG lxd hsm-worker
usermod -aG lxd hsm-collector
# Make sure the API user can never reach LXD, even if someone added it earlier.
gpasswd -d hsm-api lxd >/dev/null 2>&1 || true

echo "==> code to /opt/hsm"
install -d -o root -g root -m 0755 /opt/hsm
rsync -a --delete --exclude .git --exclude data --exclude .env --exclude 'node_modules' \
  --exclude '.venv*' --exclude '.pytest*' --exclude '.npm-cache' --exclude '__pycache__' \
  "$SRC/" /opt/hsm/
chown -R root:root /opt/hsm
[[ -x /opt/hsm/venv/bin/python ]] || python3 -m venv /opt/hsm/venv
/opt/hsm/venv/bin/pip install -q --upgrade pip
/opt/hsm/venv/bin/pip install -q -r /opt/hsm/backend/requirements.txt
/opt/hsm/venv/bin/pip install -q --no-deps /opt/hsm/backend

echo "==> configuration /etc/hsm/hsm.env"
install -d -o root -g hsm -m 0750 /etc/hsm
API_UID=$(id -u hsm-api)
{
  grep -v -E '^(HSM_DATA_DIR|SQLITE_DB_PATH|TINYFLUX_DB_PATH|HSM_HISTORY_SOCKET|HSM_HISTORY_ALLOWED_UIDS|HSM_DASHBOARD_DIST)=' "$SRC/.env"
  echo "HSM_DATA_DIR=/var/lib/hsm"
  echo "HSM_HISTORY_SOCKET=/run/hsm-collector/history.sock"
  echo "HSM_HISTORY_ALLOWED_UIDS=$API_UID"
  echo "HSM_DASHBOARD_DIST=/opt/hsm/dashboard/dist"
} > /etc/hsm/hsm.env.new
install -o root -g hsm -m 0640 /etc/hsm/hsm.env.new /etc/hsm/hsm.env
rm -f /etc/hsm/hsm.env.new

echo "==> state directories"
install -d -o root -g hsm -m 2770 /var/lib/hsm
install -d -o hsm-collector -g hsm-collector -m 0700 /var/lib/hsm/metrics

echo "==> database migration (explicit, once, before services start)"
sudo -u hsm-worker sh -c 'umask 007; HSM_ENV_FILE=/etc/hsm/hsm.env /opt/hsm/venv/bin/hsm init-db'

echo "==> systemd units"
install -m 0644 /opt/hsm/deploy/systemd/hsm-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hsm-collector.service hsm-worker.service hsm-api.service
sleep 3
systemctl --no-pager --lines=0 status hsm-collector hsm-worker hsm-api || true

echo
echo "Check that the API user cannot reach LXD (expect 'Permission denied'):"
echo "  sudo -u hsm-api /opt/hsm/venv/bin/python -c \"import socket; s=socket.socket(socket.AF_UNIX); s.connect('/var/snap/lxd/common/lxd/unix.socket')\""
echo "First admin:"
echo "  sudo -u hsm-worker env HSM_ENV_FILE=/etc/hsm/hsm.env /opt/hsm/venv/bin/hsm bootstrap"
