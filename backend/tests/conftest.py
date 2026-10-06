"""Shared fixtures: an isolated config, a migrated database and user factories."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hsm import config as config_mod  # noqa: E402
from hsm import db  # noqa: E402
from hsm.security import new_id  # noqa: E402
from hsm.services import state  # noqa: E402
from hsm.timeutil import utcnow_iso  # noqa: E402

GIB = 1024 ** 3


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("HSM_", "GOOGLE_", "SESSION_", "LXD_", "METRICS_", "EXEC_", "BOOTSTRAP_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HSM_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("HSM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("HSM_DASHBOARD_DIST", str(tmp_path / "dist"))
    return config_mod.load()


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.sqlite_path)
    db.migrate(c)
    yield c
    c.close()


def make_user(conn, email, role="user", status="active", quota=(4, 4 * GIB, 20 * GIB), sub=None):
    uid = new_id()
    now = utcnow_iso()
    conn.execute(
        "INSERT INTO users(id, google_sub, email, display_name, role, status, quota_cpu_cores,"
        " quota_memory_bytes, quota_disk_bytes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (uid, sub if sub is not None else (f"sub-{uid}" if status == "active" else None), email, email,
         role, status, quota[0], quota[1], quota[2], now, now))
    return uid


def make_container(conn, name, owner_id=None, managed=True, cpu=1, mem=GIB, disk=2 * GIB, pool="hsm-btrfs",
                   status="active", safety="safe"):
    cid = new_id()
    conn.execute(
        "INSERT INTO containers(id, project, name, lxd_marker, managed, status, safety, owner_id, cpu_cores,"
        " cpu_allowance_pct, memory_bytes, disk_bytes, pool, observed_cpu_cores, observed_memory_bytes,"
        " observed_disk_bytes, observed_pool, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "hsm", name, cid if managed else None, 1 if managed else 0, status, safety, owner_id,
         cpu if managed else None, 100 if managed else None, mem if managed else None,
         disk if managed else None, pool if managed else None, cpu, mem, disk, pool, utcnow_iso()))
    return cid


def grant(conn, container_id, user_id):
    conn.execute("INSERT INTO container_access(container_id, user_id, granted_at) VALUES (?,?,?)",
                 (container_id, user_id, utcnow_iso()))


def publish_capabilities(conn, cpu=8, mem=16 * GIB, pool_total=100 * GIB, pool_used=5 * GIB):
    state.put(conn, state.LXD_CAPABILITIES, {
        "observed_at": utcnow_iso(),
        "host": {"cpu_count": cpu, "memory_bytes": mem},
        "pools": [{"name": "hsm-btrfs", "driver": "btrfs", "total_bytes": pool_total,
                   "used_bytes": pool_used, "quota_capable": True}],
        "networks": [{"name": "hsmbr0", "type": "bridge", "managed": True}],
        "images": [{"alias": "hsm/alpine-3.22", "fingerprint": "f" * 64, "description": "Alpine 3.22",
                    "os": "Alpine", "release": "3.22", "architecture": "x86_64", "size_bytes": 3_000_000}],
        "project": {"name": "hsm", "exists": True, "restricted": True},
    })
