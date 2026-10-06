#!/usr/bin/env bash
# Prepare an Ubuntu 22.04+/WSL2 host for Hobby Server Monitor development.
#
# Run once with sudo:   sudo bash scripts/setup-dev-host.sh
#
# What it does (idempotent; it never deletes or re-initializes existing LXD
# objects):
#   1. apt: python3-venv, python3-pip, btrfs-progs (Python venv + btrfs tooling)
#   2. snap: LXD 5.21 LTS (skipped if LXD is already installed)
#   3. adds the invoking user to the `lxd` group -- DEVELOPMENT ONLY. Membership
#      of `lxd` is root-equivalent; production uses dedicated service users
#      (see deploy/install.sh).
#   4. creates, only if missing:
#        - storage pool  hsm-btrfs  (btrfs, loop-backed, quota-capable)
#        - network       hsmbr0     (managed bridge with NAT)
#        - project       hsm        (restricted: unprivileged only, no nesting,
#                                    no low-level options, managed disks/NICs
#                                    only, network access limited to hsmbr0)
#        - root + eth0 devices on the hsm project's default profile
#   5. caches two images in the local image store with hsm/* aliases.
set -euo pipefail

POOL=${HSM_POOL:-hsm-btrfs}
POOL_SIZE=${HSM_POOL_SIZE:-30GiB}
BRIDGE=${HSM_BRIDGE:-hsmbr0}
PROJECT=${HSM_PROJECT:-hsm}
LXD_CHANNEL=${HSM_LXD_CHANNEL:-5.21/stable}

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo: sudo bash $0" >&2
  exit 1
fi
TARGET_USER=${SUDO_USER:-}

echo "==> apt packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip btrfs-progs ca-certificates curl

echo "==> LXD"
if ! snap list lxd >/dev/null 2>&1; then
  snap install lxd --channel="$LXD_CHANNEL"
else
  echo "LXD already installed: $(snap list lxd | tail -1 | awk '{print $2, $4}')"
fi
lxd waitready --timeout=120

if [[ -n "$TARGET_USER" ]] && ! id -nG "$TARGET_USER" | grep -qw lxd; then
  usermod -aG lxd "$TARGET_USER"
  echo "added $TARGET_USER to group lxd (development only; root-equivalent). Open a new shell for it to apply."
fi

echo "==> storage pool $POOL"
if ! lxc storage show "$POOL" >/dev/null 2>&1; then
  modprobe btrfs 2>/dev/null || true
  lxc storage create "$POOL" btrfs size="$POOL_SIZE"
else
  echo "pool exists: $(lxc storage get "$POOL" driver 2>/dev/null || lxc storage list -f csv | grep "^$POOL,")"
fi

echo "==> network $BRIDGE"
if ! lxc network show "$BRIDGE" >/dev/null 2>&1; then
  lxc network create "$BRIDGE" ipv4.address=auto ipv4.nat=true ipv6.address=none
else
  echo "network exists"
fi

echo "==> project $PROJECT"
if ! lxc project show "$PROJECT" >/dev/null 2>&1; then
  lxc project create "$PROJECT"
  # features.* must be set while the project is still empty.
  lxc project set "$PROJECT" features.images=false
  lxc project set "$PROJECT" features.profiles=true
  lxc project set "$PROJECT" features.storage.volumes=true
  # restricted=true already defaults every restricted.* key to its safe value;
  # they are spelled out so the policy is reviewable and survives default changes.
  for kv in \
    restricted=true \
    restricted.containers.nesting=block \
    restricted.containers.privilege=unprivileged \
    restricted.containers.lowlevel=block \
    restricted.containers.interception=block \
    restricted.devices.disk=managed \
    restricted.devices.nic=managed \
    restricted.devices.proxy=block \
    restricted.devices.gpu=block \
    restricted.devices.usb=block \
    restricted.devices.pci=block \
    restricted.devices.unix-char=block \
    restricted.devices.unix-block=block \
    restricted.devices.unix-hotplug=block \
    restricted.devices.infiniband=block \
    restricted.virtual-machines.lowlevel=block \
    restricted.networks.access="$BRIDGE"; do
    lxc project set "$PROJECT" "$kv"
  done
else
  echo "project exists (configuration left untouched)"
fi

if ! lxc profile device show default --project "$PROJECT" | grep -q '^root:'; then
  lxc profile device add default root disk path=/ pool="$POOL" --project "$PROJECT"
fi
if ! lxc profile device show default --project "$PROJECT" | grep -q '^eth0:'; then
  lxc profile device add default eth0 nic network="$BRIDGE" name=eth0 --project "$PROJECT"
fi

echo "==> images (local cache, aliases under hsm/)"
copy_image() {  # remote:alias -> local alias
  local src=$1 alias=$2
  if lxc image info "$alias" >/dev/null 2>&1; then
    echo "image $alias already cached"
  else
    lxc image copy "$src" local: --alias "$alias" --auto-update || echo "WARN: could not fetch $src"
  fi
}
copy_image ubuntu:24.04 hsm/ubuntu-24.04
copy_image images:alpine/3.22 hsm/alpine-3.22

echo
echo "Done. Summary:"
lxc --version
lxc storage list -f csv
lxc network list -f csv | grep "^$BRIDGE," || true
lxc project list -f csv | grep "^$PROJECT" || true
lxc image list -f csv -c lfs
