#!/usr/bin/env bash
# Verify the LXD privilege boundary of an installed deployment.
#
#   sudo bash deploy/check-permissions.sh
#
# 1. hsm-api must NOT be able to connect to the LXD socket.
# 2. hsm-worker / hsm-collector must be able to, both as plain users and inside
#    a transient unit that carries their systemd sandbox options. If the
#    sandboxed attempt fails, each option is tried alone to find the culprit.
set -u
SOCK=${LXD_SOCKET:-/var/snap/lxd/common/lxd/unix.socket}
PY=/opt/hsm/venv/bin/python
PROBE="import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(3)
try:
    s.connect('$SOCK'); print('CONNECTED')
except OSError as e:
    print('DENIED', e.errno, e.strerror); sys.exit(1)"
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

plain() { sudo -u "$1" "$PY" -c "$PROBE" 2>&1; }
sandboxed() {   # user, then extra -p properties
  local u=$1; shift
  systemd-run --quiet --wait --pipe --collect -p User="$u" -p Group=hsm -p SupplementaryGroups=lxd "$@" \
    "$PY" -c "$PROBE" 2>&1
}

echo "== 1. API user (expect DENIED)"
echo "hsm-api plain: $(plain hsm-api)"
echo "== 2. worker/collector as plain users (expect CONNECTED)"
for u in hsm-worker hsm-collector; do echo "$u plain: $(plain "$u")"; done

UNIT_PROPS=(-p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes
            -p ProtectKernelTunables=yes -p ProtectKernelModules=yes -p ProtectControlGroups=yes
            -p RestrictSUIDSGID=yes -p ReadWritePaths=/var/lib/hsm)
echo "== 3. collector inside its unit sandbox (expect CONNECTED)"
r=$(sandboxed hsm-collector "${UNIT_PROPS[@]}"); echo "all options: $r"
if [[ $r != CONNECTED* ]]; then
  echo "== 4. bisect: each option alone"
  for p in NoNewPrivileges=yes ProtectSystem=strict ProtectSystem=full ProtectHome=yes PrivateTmp=yes \
           ProtectKernelTunables=yes ProtectKernelModules=yes ProtectControlGroups=yes RestrictSUIDSGID=yes; do
    echo "$p: $(sandboxed hsm-collector -p "$p")"
  done
fi
echo "== 5. running services"
for s in hsm-api hsm-worker hsm-collector; do echo "$s: $(systemctl is-active $s)"; done
journalctl -u hsm-collector -n 3 --no-pager | tail -3
