"""Operator commands. These run locally with filesystem access to the database,
which is already full control of the application, so they are not an
authentication bypass for remote users.

    hsm init-db                     create/upgrade the SQLite schema
    hsm bootstrap                   print the one-time first-admin setup link
    hsm recover-admin --email E     promote an existing active user to admin (audited)
    hsm test-session --email E      local-http only: print a session cookie for browser tests
    hsm serve-dev                   development WSGI server (production uses gunicorn)
"""
from __future__ import annotations

import argparse
import sys

from . import config as config_mod
from . import db


def _conn(cfg):
    conn = db.connect(cfg.sqlite_path)
    if db.schema_version(conn) == 0:
        sys.exit("database is not initialized; run `hsm init-db` first")
    return conn


def cmd_init_db(cfg, args) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(cfg.sqlite_path)
    applied = db.migrate(conn)
    print(f"database {cfg.sqlite_path}: schema version {db.schema_version(conn)}"
          + (f" (applied {applied})" if applied else " (up to date)"))


def cmd_bootstrap(cfg, args) -> None:
    from .auth.admission import issue_bootstrap_secret
    try:
        secret = issue_bootstrap_secret(_conn(cfg), cfg)
    except RuntimeError as exc:
        sys.exit(f"bootstrap refused: {exc}")
    print("One-time setup link for", cfg.bootstrap_admin_email, "(valid 30 minutes, shown once):")
    print(f"  {cfg.public_base_url}/setup/#{secret}")


def cmd_recover_admin(cfg, args) -> None:
    from .db import write_tx
    from .security import canonical_email
    from .services import audit
    from .timeutil import utcnow_iso
    conn = _conn(cfg)
    email = canonical_email(args.email)
    with write_tx(conn):
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None or user["status"] != "active":
            sys.exit("recover-admin needs an existing *active* user; invite them first or use bootstrap")
        conn.execute("UPDATE users SET role = 'admin', updated_at = ? WHERE id = ?", (utcnow_iso(), user["id"]))
        audit.record(conn, action="user.recover_admin", outcome="succeeded", actor_email="local-operator",
                     target_type="user", target_id=user["id"], target_label=email)
    print(f"{email} is now an admin")


def cmd_test_session(cfg, args) -> None:
    from .auth import sessions
    from .db import write_tx
    from .security import canonical_email
    from .services import audit
    if cfg.deployment_mode != "local-http":
        sys.exit("test-session is only available in local-http development mode")
    conn = _conn(cfg)
    with write_tx(conn):
        user = conn.execute("SELECT * FROM users WHERE email = ? AND status = 'active'",
                            (canonical_email(args.email),)).fetchone()
        if user is None:
            sys.exit("no active user with that email")
        token = sessions.create(conn, cfg, user["id"])
        audit.record(conn, action="auth.test_session", outcome="succeeded", actor_email="local-operator",
                     target_type="user", target_id=user["id"], target_label=user["email"])
    print(f"{sessions.cookie_name(cfg)}={token}")


def cmd_serve_dev(cfg, args) -> None:
    import logging
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

    from .app import create_app

    class Server(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    class QuietHandler(WSGIRequestHandler):
        def log_request(self, code="-", size="-"):
            # The path only: query strings never carry secrets here, but keep logs small.
            logging.getLogger("hsm.dev").info("%s %s %s", self.command, self.path.split("?")[0], code)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    httpd = make_server(cfg.backend_host, cfg.backend_port, create_app(cfg), server_class=Server,
                        handler_class=QuietHandler)
    print(f"dev server on http://{cfg.backend_host}:{cfg.backend_port} (not for production)")
    httpd.serve_forever()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="hsm")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("bootstrap")
    p = sub.add_parser("recover-admin")
    p.add_argument("--email", required=True)
    p = sub.add_parser("test-session")
    p.add_argument("--email", required=True)
    sub.add_parser("serve-dev")
    args = parser.parse_args(argv)
    cfg = config_mod.load()
    {"init-db": cmd_init_db, "bootstrap": cmd_bootstrap, "recover-admin": cmd_recover_admin,
     "test-session": cmd_test_session, "serve-dev": cmd_serve_dev}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
