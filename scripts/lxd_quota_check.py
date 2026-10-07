#!/usr/bin/env python3
"""Real-LXD check of two things the spike cannot show from its summary:

1. Root-disk quota enforcement on the btrfs pool: write past a 1 GiB root disk
   and expect the write to fail (btrfs qgroups; `df` shows the whole pool).
2. After a guest command overruns its deadline, no process of uid 1500 survives.

Works only in project `hsm`, on one container it creates (`hsm-qcheck-<random>`),
which it deletes in `finally`. Prints JSON.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from hsm import config as config_mod  # noqa: E402
from hsm.integrations.lxd import LxdAdapter  # noqa: E402

MIB = 1024 ** 2


def main() -> int:
    cfg = config_mod.load()
    assert cfg.lxd_project == "hsm"
    ad = LxdAdapter(cfg)
    name = f"hsm-qcheck-{uuid.uuid4().hex[:8]}"
    marker = str(uuid.uuid4())
    import subprocess  # read-only image lookup on the host for this test script
    fp = subprocess.run(["lxc", "image", "info", "hsm/alpine-3.22"], capture_output=True, text=True,
                        check=True).stdout.split("Fingerprint:")[1].split()[0]
    out: dict = {"name": name}
    try:
        ad.create("hsm", name, image_fingerprint=fp, pool="hsm-btrfs", network="hsmbr0", cpu_cores=1,
                  cpu_allowance_pct=100, memory_bytes=256 * MIB, disk_bytes=1024 * MIB, ephemeral=False,
                  autostart=False, description="quota check", marker=marker)
        ad.start("hsm", name)
        ad.provision_guest_user("hsm", name)
        # 1) Write 1.5 GiB into a 1 GiB root disk as root.
        r = ad.exec_command("hsm", name, "dd if=/dev/zero of=/root/fill bs=1M count=1536 2>&1; echo rc=$?; "
                                         "du -m /root/fill", as_root=True)
        out["disk_write_1536MiB_into_1GiB"] = {"exit": r["exit_code"], "stdout": r["stdout"][-400:]}
        ad.exec_command("hsm", name, "rm -f /root/fill", as_root=True)
        # 2) Guest overruns: background sleep + foreground sleep past the deadline.
        r = ad.exec_command("hsm", name, "sleep 300 & sleep 300", as_root=False)
        out["guest_overrun"] = {"outcome": r["outcome"], "exit": r["exit_code"], "duration_ms": r["duration_ms"]}
        ps = ad.exec_command("hsm", name, "ps -o user,pid,args | grep -v 'ps -o' | grep '^hsm' || echo none-left",
                             as_root=True)
        out["uid1500_processes_after"] = ps["stdout"].strip()
    finally:
        try:
            try:
                ad.stop("hsm", name, force=True)
            except Exception:
                pass
            ad.delete("hsm", name)
            out["deleted"] = True
        except Exception as exc:  # report, do not hide
            out["delete_error"] = repr(exc)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
