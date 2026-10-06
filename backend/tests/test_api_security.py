"""Authentication, CSRF and object-level authorization through the real Falcon app."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from falcon import testing

from hsm import app as app_mod
from hsm.auth import oidc, policy, sessions
from hsm.services import operations

from conftest import GIB, grant, make_container, make_user, publish_capabilities

ORIGIN = "http://localhost:8000"


@pytest.fixture
def client(cfg, conn):
    return testing.TestClient(app_mod.create_app(cfg))


def login(conn, cfg, user_id):
    token = sessions.create(conn, cfg, user_id)
    principal = sessions.resolve(conn, cfg, token)
    return {"Cookie": f"hsm_session={token}"}, principal.csrf_token


def unsafe(headers, csrf, **extra):
    return {**headers, "Origin": ORIGIN, "X-CSRF-Token": csrf, "Content-Type": "application/json", **extra}


# --------------------------------------------------------------------------- policy registry

def test_every_route_declares_a_policy():
    policy.assert_all_routes_declared(app_mod.routes())


def test_route_without_policy_fails_startup():
    class Leaky:
        policies = {"GET": policy.PUBLIC}

        def on_get(self, req, resp): ...

        def on_post(self, req, resp): ...

    with pytest.raises(RuntimeError, match="POST /leaky"):
        policy.assert_all_routes_declared([("/leaky", Leaky())])


# --------------------------------------------------------------------------- sessions and CSRF

def test_private_routes_require_session(client):
    for path in ("/api/me", "/api/containers", "/api/users", "/api/accounting", "/api/audit-events"):
        r = client.simulate_get(path)
        assert r.status_code == 401, path
        assert r.json["error"]["code"] == "UNAUTHENTICATED"
        assert r.headers["Cache-Control"] == "no-store"


def test_logout_revokes_server_session_and_old_cookie_fails(client, conn, cfg):
    uid = make_user(conn, "u@example.com")
    headers, csrf = login(conn, cfg, uid)
    assert client.simulate_get("/api/me", headers=headers).status_code == 200
    r = client.simulate_post("/auth/logout", headers=unsafe(headers, csrf))
    assert r.status_code == 204
    assert client.simulate_get("/api/me", headers=headers).status_code == 401


def test_csrf_and_origin_are_required_for_unsafe_methods(client, conn, cfg):
    uid = make_user(conn, "u@example.com")
    headers, csrf = login(conn, cfg, uid)
    r = client.simulate_post("/auth/logout", headers={**headers, "Origin": ORIGIN})
    assert r.status_code == 403 and r.json["error"]["code"] == "CSRF_FAILED"
    r = client.simulate_post("/auth/logout", headers={**headers, "X-CSRF-Token": csrf, "Origin": "https://evil.test"})
    assert r.status_code == 403 and r.json["error"]["code"] == "ORIGIN_REJECTED"
    r = client.simulate_post("/auth/logout", headers={**headers, "X-CSRF-Token": csrf})
    assert r.status_code == 403
    # Another session's CSRF token does not work either.
    _, other_csrf = login(conn, cfg, uid)
    r = client.simulate_post("/auth/logout", headers=unsafe(headers, other_csrf))
    assert r.status_code == 403


def test_revocation_is_immediate(client, conn, cfg):
    admin = make_user(conn, "admin@example.com", role="admin")
    a_headers, a_csrf = login(conn, cfg, admin)
    uid = make_user(conn, "u@example.com")
    u_headers, _ = login(conn, cfg, uid)
    assert client.simulate_get("/api/me", headers=u_headers).status_code == 200
    r = client.simulate_post(f"/api/users/{uid}/revoke", headers=unsafe(a_headers, a_csrf))
    assert r.status_code == 200, r.text
    assert client.simulate_get("/api/me", headers=u_headers).status_code == 401


def test_demotion_takes_effect_on_next_request(client, conn, cfg):
    admin = make_user(conn, "admin@example.com", role="admin")
    other = make_user(conn, "admin2@example.com", role="admin")
    o_headers, _ = login(conn, cfg, other)
    assert client.simulate_get("/api/users", headers=o_headers).status_code == 200
    a_headers, a_csrf = login(conn, cfg, admin)
    r = client.simulate_patch(f"/api/users/{other}", json={"role": "user"}, headers=unsafe(a_headers, a_csrf))
    assert r.status_code == 200, r.text
    assert client.simulate_get("/api/users", headers=o_headers).status_code == 403


def test_last_admin_cannot_be_demoted_or_revoked(client, conn, cfg):
    admin = make_user(conn, "admin@example.com", role="admin")
    headers, csrf = login(conn, cfg, admin)
    r = client.simulate_patch(f"/api/users/{admin}", json={"role": "user"}, headers=unsafe(headers, csrf))
    assert r.status_code == 409 and r.json["error"]["code"] == "LAST_ADMIN"
    r = client.simulate_post(f"/api/users/{admin}/revoke", headers=unsafe(headers, csrf))
    assert r.status_code == 409 and r.json["error"]["code"] == "LAST_ADMIN"


# --------------------------------------------------------------------------- object authorization

@pytest.fixture
def two_users(conn, cfg):
    owner_a = make_user(conn, "a@example.com")
    owner_b = make_user(conn, "b@example.com")
    ca = make_container(conn, "ca", owner_a)
    cb = make_container(conn, "cb", owner_b)
    grant(conn, ca, owner_a)
    grant(conn, cb, owner_b)
    conn.execute("INSERT INTO latest_metrics(container_id, state, sampled_at) VALUES (?, 'Running', ?)",
                 (cb, "2026-01-01T00:00:00Z"))
    return owner_a, owner_b, ca, cb


def test_user_lists_only_assigned_containers(client, conn, cfg, two_users):
    owner_a, _, ca, cb = two_users
    headers, _ = login(conn, cfg, owner_a)
    ids = [c["id"] for c in client.simulate_get("/api/containers", headers=headers).json["containers"]]
    assert ids == [ca]


def test_unassigned_container_routes_return_403(client, conn, cfg, two_users):
    owner_a, owner_b, _, cb = two_users
    headers, csrf = login(conn, cfg, owner_a)
    for path in (f"/api/containers/{cb}", f"/api/containers/{cb}/history?range=1h",
                 f"/api/containers/{cb}/usage?range=24h"):
        r = client.simulate_get(path, headers=headers)
        assert r.status_code == 403, (path, r.text)
    r = client.simulate_post(f"/api/containers/{cb}/exec", json={"command": "id"},
                             headers=unsafe(headers, csrf, **{"Idempotency-Key": "k" * 16}))
    assert r.status_code == 403
    # Admin-only mutations are 403 for users even on assigned containers.
    r = client.simulate_post(f"/api/containers/{cb}/actions", json={"action": "stop"},
                             headers=unsafe(headers, csrf, **{"Idempotency-Key": "k" * 16}))
    assert r.status_code == 403


def test_unknown_container_is_404(client, conn, cfg, two_users):
    owner_a, *_ = two_users
    headers, _ = login(conn, cfg, owner_a)
    assert client.simulate_get("/api/containers/00000000-0000-4000-8000-000000000000",
                               headers=headers).status_code == 404
    assert client.simulate_get("/api/containers/not-a-uuid", headers=headers).status_code == 404


def test_operation_results_are_private(client, conn, cfg, two_users):
    owner_a, owner_b, _, cb = two_users
    op, _ = operations.submit(conn, cfg, actor_id=owner_b, kind="exec", container_id=cb,
                              payload={"command": "cat secret", "as_root": False}, idempotency_key="key-12345678")
    operations.set_state(conn, op["id"], "succeeded", result={"stdout": "s3cret"}, result_ttl_minutes=5)
    a_headers, _ = login(conn, cfg, owner_a)
    assert client.simulate_get(f"/api/operations/{op['id']}", headers=a_headers).status_code == 403
    b_headers, _ = login(conn, cfg, owner_b)
    r = client.simulate_get(f"/api/operations/{op['id']}", headers=b_headers)
    assert r.status_code == 200 and r.json["operation"]["result"] == {"stdout": "s3cret"}
    # Admin sees status but not another user's command output.
    admin = make_user(conn, "admin@example.com", role="admin")
    ad_headers, _ = login(conn, cfg, admin)
    r = client.simulate_get(f"/api/operations/{op['id']}", headers=ad_headers)
    assert r.status_code == 200 and r.json["operation"]["result"] is None
    # After losing access, the actor cannot read the result either.
    conn.execute("DELETE FROM container_access WHERE user_id = ?", (owner_b,))
    assert client.simulate_get(f"/api/operations/{op['id']}", headers=b_headers).status_code == 403


def test_user_cannot_reach_admin_routes(client, conn, cfg):
    uid = make_user(conn, "u@example.com")
    headers, csrf = login(conn, cfg, uid)
    for path in ("/api/users", "/api/accounting", "/api/audit-events", "/api/creation-options"):
        assert client.simulate_get(path, headers=headers).status_code == 403, path
    r = client.simulate_post("/api/invitations", json={"email": "x@example.com", "role": "admin"},
                             headers=unsafe(headers, csrf))
    assert r.status_code == 403


# --------------------------------------------------------------------------- Google login / admission

def _start(client, path="/auth/google/start", **kw):
    r = client.simulate_get(path, **kw) if path.endswith("start") else client.simulate_post(path, **kw)
    return r


def _callback(client, monkeypatch, authorize_url, binding, claims):
    state = parse_qs(urlparse(authorize_url).query)["state"][0]
    nonce = parse_qs(urlparse(authorize_url).query)["nonce"][0]
    full = {"iss": "https://accounts.google.com", "aud": "client-id.apps.googleusercontent.com",
            "email_verified": True, "nonce": nonce, **claims}
    monkeypatch.setattr(oidc, "exchange_code", lambda cfg, code, verifier: "id-token")
    monkeypatch.setattr(oidc, "verify_id_token", lambda cfg, token: full)
    return client.simulate_get("/auth/google/callback", params={"state": state, "code": "c"},
                               headers={"Cookie": f"hsm_oauth={binding}"})


def _plain_login(client, monkeypatch, claims):
    r = client.simulate_get("/auth/google/start")
    assert r.status_code == 302
    url = r.headers["Location"]
    assert parse_qs(urlparse(url).query)["code_challenge_method"] == ["S256"]
    return _callback(client, monkeypatch, url, r.cookies["hsm_oauth"].value, claims)


def test_uninvited_google_account_is_denied(client, monkeypatch):
    r = _plain_login(client, monkeypatch, {"sub": "999", "email": "stranger@example.com"})
    assert r.status_code == 302 and r.headers["Location"] == "/login/?error=NOT_INVITED"
    assert "hsm_session" not in r.cookies


def test_returning_user_matched_by_sub_not_email(client, conn, monkeypatch):
    make_user(conn, "u@example.com", sub="sub-1")
    r = _plain_login(client, monkeypatch, {"sub": "sub-2", "email": "u@example.com"})
    assert r.headers["Location"] == "/login/?error=NOT_INVITED"
    r = _plain_login(client, monkeypatch, {"sub": "sub-1", "email": "changed@example.com"})
    assert r.headers["Location"] == "/" and "hsm_session" in r.cookies


def test_unverified_email_and_nonce_mismatch_are_rejected(client, conn, monkeypatch):
    make_user(conn, "u@example.com", sub="sub-1")
    r = _plain_login(client, monkeypatch, {"sub": "sub-1", "email": "u@example.com", "email_verified": False})
    assert r.headers["Location"] == "/login/?error=EMAIL_UNVERIFIED"
    r = _plain_login(client, monkeypatch, {"sub": "sub-1", "email": "u@example.com", "nonce": "forged"})
    assert r.headers["Location"] == "/login/?error=OAUTH_FAILED"
    r = _plain_login(client, monkeypatch, {"sub": "sub-1", "email": "u@example.com", "aud": "other-client"})
    assert r.headers["Location"] == "/login/?error=OAUTH_FAILED"


def test_state_is_single_use_and_browser_bound(client, conn, monkeypatch):
    make_user(conn, "u@example.com", sub="sub-1")
    r = client.simulate_get("/auth/google/start")
    url, binding = r.headers["Location"], r.cookies["hsm_oauth"].value
    claims = {"sub": "sub-1", "email": "u@example.com"}
    assert _callback(client, monkeypatch, url, "other-browser", claims).headers["Location"] == "/login/?error=STATE_INVALID"
    # The failed attempt consumed the transaction, so even the right browser cannot reuse it.
    assert _callback(client, monkeypatch, url, binding, claims).headers["Location"] == "/login/?error=STATE_INVALID"


def _invite(client, conn, cfg, email="new@example.com"):
    admin = make_user(conn, "admin@example.com", role="admin")
    headers, csrf = login(conn, cfg, admin)
    r = client.simulate_post("/api/invitations", headers=unsafe(headers, csrf),
                             json={"email": email, "role": "user",
                                   "quota": {"cpu_cores": 2, "memory_bytes": GIB, "disk_bytes": 4 * GIB}})
    assert r.status_code == 201, r.text
    secret = r.json["link"].split("#", 1)[1]
    stored = conn.execute("SELECT token_hash FROM invitations").fetchone()[0]
    assert secret not in stored        # only the hash is stored
    return secret


def _admission(client, kind, secret):
    return client.simulate_post("/auth/admission-context", json={"kind": kind, "secret": secret},
                                headers={"Origin": ORIGIN})


def test_invitation_admits_once_and_only_for_the_invited_email(client, conn, cfg, monkeypatch):
    secret = _invite(client, conn, cfg)
    r = _admission(client, "invitation", secret)
    assert r.status_code == 200, r.text
    wrong = _callback(client, monkeypatch, r.json["authorize_url"], r.cookies["hsm_oauth"].value,
                      {"sub": "g-1", "email": "someone-else@example.com"})
    assert wrong.headers["Location"] == "/login/?error=EMAIL_MISMATCH"
    r = _admission(client, "invitation", secret)
    ok = _callback(client, monkeypatch, r.json["authorize_url"], r.cookies["hsm_oauth"].value,
                   {"sub": "g-1", "email": "new@example.com"})
    assert ok.headers["Location"] == "/" and "hsm_session" in ok.cookies
    assert conn.execute("SELECT status, google_sub FROM users WHERE email = 'new@example.com'").fetchone()[:] == \
        ("active", "g-1")
    replay = _admission(client, "invitation", secret)
    assert replay.status_code == 400 and replay.json["error"]["code"] == "INVITATION_INVALID"


def test_admission_context_rejects_cross_origin(client, conn, cfg):
    secret = _invite(client, conn, cfg)
    r = client.simulate_post("/auth/admission-context", json={"kind": "invitation", "secret": secret},
                             headers={"Origin": "https://evil.test"})
    assert r.status_code == 403


def test_bootstrap_requires_secret_and_email_and_cannot_be_replayed(client, conn, cfg, monkeypatch):
    from hsm.auth.admission import issue_bootstrap_secret
    secret = issue_bootstrap_secret(conn, cfg)
    r = _admission(client, "bootstrap", secret)
    wrong = _callback(client, monkeypatch, r.json["authorize_url"], r.cookies["hsm_oauth"].value,
                      {"sub": "g-x", "email": "attacker@example.com"})
    assert wrong.headers["Location"] == "/login/?error=EMAIL_MISMATCH"
    r = _admission(client, "bootstrap", secret)
    ok = _callback(client, monkeypatch, r.json["authorize_url"], r.cookies["hsm_oauth"].value,
                   {"sub": "g-admin", "email": "admin@example.com"})
    assert ok.headers["Location"] == "/"
    assert conn.execute("SELECT role FROM users WHERE google_sub = 'g-admin'").fetchone()[0] == "admin"
    assert _admission(client, "bootstrap", secret).status_code == 400
    with pytest.raises(RuntimeError):
        issue_bootstrap_secret(conn, cfg)


def test_admission_context_is_rate_limited(client):
    codes = [_admission(client, "invitation", "x" * 43).status_code for _ in range(12)]
    assert codes[:10] == [400] * 10 and codes[-1] == 429


# --------------------------------------------------------------------------- creation validation

def test_create_rejects_unknown_fields_and_bad_names(client, conn, cfg):
    admin = make_user(conn, "admin@example.com", role="admin")
    publish_capabilities(conn)
    headers, csrf = login(conn, cfg, admin)
    base = {"name": "web-1", "image": "hsm/alpine-3.22", "pool": "hsm-btrfs", "network": "hsmbr0",
            "cpu_cores": 1, "cpu_allowance_pct": 50, "memory_bytes": 256 * 1024 * 1024,
            "disk_bytes": 2 * GIB, "owner_id": admin}
    h = unsafe(headers, csrf, **{"Idempotency-Key": "create-0001"})
    r = client.simulate_post("/api/containers", json={**base, "raw.lxc": "lxc.apparmor.profile=unconfined"}, headers=h)
    assert r.status_code == 422 and "raw.lxc" in r.json["error"]["fields"]
    r = client.simulate_post("/api/containers", json={**base, "name": "Bad_Name"}, headers=h)
    assert r.status_code == 422
    r = client.simulate_post("/api/containers", json={**base, "image": "images:evil/latest"}, headers=h)
    assert r.status_code == 422
    r = client.simulate_post("/api/containers", json={**base, "pool": "default"}, headers=h)
    assert r.status_code == 422
    r = client.simulate_post("/api/containers", json=base, headers=h)
    assert r.status_code == 202, r.text
    replay = client.simulate_post("/api/containers", json=base, headers=h)
    assert replay.status_code == 200 and replay.json["operation"]["id"] == r.json["operation"]["id"]
    r = client.simulate_post("/api/containers", json={**base, "cpu_cores": 2}, headers=h)
    assert r.status_code == 409 and r.json["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_static_paths_cannot_escape_dist(client, cfg):
    cfg.dashboard_dist.mkdir(parents=True, exist_ok=True)
    (cfg.dashboard_dist / "index.html").write_text("<h1>ok</h1>")
    assert client.simulate_get("/").text == "<h1>ok</h1>"
    assert client.simulate_get("/../config.py").status_code == 404
    assert client.simulate_get("/%2e%2e/%2e%2e/etc/passwd").status_code == 404
    assert client.simulate_post("/").status_code == 405


def test_history_accepts_comma_separated_metrics(client, conn, cfg, two_users, monkeypatch):
    from hsm import history_client
    owner_a, _, ca, _ = two_users
    seen = {}
    monkeypatch.setattr(history_client, "query", lambda cfg, req, timeout=5.0: seen.update(req) or {"ok": 1})
    headers, _ = login(conn, cfg, owner_a)
    r = client.simulate_get(f"/api/containers/{ca}/history",
                            params={"range": "1h", "metrics": "cpu_pct,memory_bytes,rx_rate", "max_points": "300"},
                            headers=headers)
    assert r.status_code == 200, r.text
    assert seen["metrics"] == ["cpu_pct", "memory_bytes", "rx_rate"] and seen["user_id"] == owner_a
    r = client.simulate_get(f"/api/containers/{ca}/history", params={"range": "1h", "metrics": "cpu_pct,evil"},
                            headers=headers)
    assert r.status_code == 422
