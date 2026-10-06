from hsm import db


def test_migrate_from_empty_is_idempotent(cfg):
    conn = db.connect(cfg.sqlite_path)
    assert db.migrate(conn) == [1]
    assert db.migrate(conn) == []
    assert db.schema_version(conn) == 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "sessions", "containers", "operations", "quota_reservations", "latest_metrics",
            "audit_events", "service_state", "oauth_transactions", "invitations"} <= tables


def test_write_tx_rolls_back(conn):
    try:
        with db.write_tx(conn):
            conn.execute("INSERT INTO service_state(key, value, updated_at) VALUES ('k', '1', 'x')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM service_state").fetchone()[0] == 0
