"""LxdAdapter: config construction, error/outcome mapping and request shapes,
against a recording fake of pylxd's ``client.api`` node tree (no LXD needed)."""
from __future__ import annotations

import pytest
import requests
import urllib3
from pylxd import exceptions as px

from hsm import lxd_safety
from hsm.integrations import lxd
from hsm.integrations.lxd_errors import (AdapterRefused, AmbiguousIdentity, LxdNotFound, LxdRejected,
                                         LxdUncertain, LxdUnavailable)

GIB = 1024 ** 3
MIB = 1024 ** 2


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.content = body, status, b""

    def json(self):
        return self._body


class Node:
    def __init__(self, client, path):
        self._client, self._path = client, path

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return Node(self._client, f"{self._path}/{name.replace('storage_pools', 'storage-pools')}")

    def __getitem__(self, item):
        return Node(self._client, f"{self._path}/{item}")

    def _call(self, method, kw):
        self._client.calls.append((method, self._path, kw))
        return self._client.handler(method, self._path, kw)

    def get(self, **kw):
        return self._call("GET", kw)

    def post(self, **kw):
        return self._call("POST", kw)

    def put(self, **kw):
        return self._call("PUT", kw)

    def patch(self, **kw):
        return self._call("PATCH", kw)

    def delete(self, **kw):
        return self._call("DELETE", kw)


class FakeClient:
    def __init__(self, handler):
        self.calls, self.handler = [], handler
        self.api = Node(self, "")


def async_resp():
    return Resp({"type": "async", "operation": "/1.0/operations/op-1", "metadata": {}}, 202)


def wait_resp(code=200, status="Success", err=""):
    return Resp({"type": "sync", "metadata": {"status_code": code, "status": status, "err": err}})


def raw_instance(name, *, project="hsm", marker=None, profile_marker=None, root_size="2048MiB", status="Running"):
    cfg = {"volatile.uuid": f"uuid-{name}"}
    if marker:
        cfg["user.hsm.id"] = marker
    ecfg = dict(cfg)
    if profile_marker and not marker:
        ecfg["user.hsm.id"] = profile_marker
    devices = {"root": {"type": "disk", "path": "/", "pool": "hsm-btrfs", "size": root_size},
               "eth0": {"type": "nic", "network": "hsmbr0", "name": "eth0"}}
    return {"name": name, "project": project, "status": status, "type": "container", "config": cfg,
            "expanded_config": ecfg, "devices": devices, "expanded_devices": devices}


@pytest.fixture
def make_adapter(cfg):
    def make(handler):
        client = FakeClient(handler)
        seen = []

        def factory(**kw):
            seen.append(kw)
            return client
        adapter = lxd.LxdAdapter(cfg, client_factory=factory)
        adapter._seen = seen
        return adapter, client
    return make


# ------------------------------------------------------------ pure config

def test_allowance_is_hard_cfs_quota():
    assert lxd.allowance(2, 50) == "100ms/100ms"
    assert lxd.allowance(1, 10) == "10ms/100ms"
    assert lxd.allowance(4, 100) == "400ms/100ms"


def create_kwargs(**over):
    kw = dict(image_fingerprint="a" * 64, pool="hsm-btrfs", network="hsmbr0", cpu_cores=2, cpu_allowance_pct=50,
              memory_bytes=512 * MIB + 123, disk_bytes=2 * GIB, ephemeral=False, autostart=True,
              description="d", marker="0b6f4c1e-1111-4222-8333-444455556666")
    kw.update(over)
    return kw


def test_create_body_is_the_fixed_safe_template(cfg):
    body = lxd.build_create_body(cfg, "web-1", **create_kwargs())
    c = body["config"]
    assert c == {"limits.cpu": "2", "limits.cpu.allowance": "100ms/100ms", "limits.memory": "512MiB",
                 "limits.memory.swap": "false", "limits.processes": str(cfg.container_max_processes),
                 "boot.autostart": "true", "security.privileged": "false", "security.nesting": "false",
                 "user.hsm.id": "0b6f4c1e-1111-4222-8333-444455556666"}
    assert not [k for k in c if k.startswith("raw.")]
    assert body["devices"] == {"root": {"type": "disk", "path": "/", "pool": "hsm-btrfs", "size": "2048MiB"},
                               "eth0": {"type": "nic", "network": "hsmbr0", "name": "eth0"}}
    assert body["profiles"] == ["default"]
    assert body["source"] == {"type": "image", "fingerprint": "a" * 64}
    assert set(body) == {"name", "type", "description", "ephemeral", "profiles", "source", "config", "devices"}
    inst = {"type": "container", "expanded_config": c, "expanded_devices": body["devices"]}
    assert lxd_safety.evaluate(inst, allowed_pools=cfg.allowed_pools, allowed_networks=cfg.allowed_networks) == []


@pytest.mark.parametrize("over", [
    {"pool": "default"}, {"network": "lxdbr0"}, {"image_fingerprint": "abc"}, {"cpu_allowance_pct": 0},
    {"cpu_allowance_pct": 150}, {"cpu_cores": 0}, {"cpu_cores": True}, {"memory_bytes": "1G"},
    {"ephemeral": "no"}, {"marker": "x"}, {"description": "x" * 2000},
])
def test_create_body_rejects_bad_values(cfg, over):
    with pytest.raises(AdapterRefused):
        lxd.build_create_body(cfg, "web-1", **create_kwargs(**over))


@pytest.mark.parametrize("name", ["", "Web", "1abc", "a" * 64, "a/b", "../x", "x-"])
def test_create_body_rejects_bad_names(cfg, name):
    with pytest.raises(AdapterRefused):
        lxd.build_create_body(cfg, name, **create_kwargs())


def test_parse_size():
    assert lxd.parse_size("2048MiB") == 2 * GIB
    assert lxd.parse_size("2GiB") == 2 * GIB
    assert lxd.parse_size("10GB") == 10 * 1000 ** 3
    assert lxd.parse_size("1024") == 1024
    assert lxd.parse_size("50%") is None
    assert lxd.parse_size(None) is None


def test_limits_match():
    inst = {"expanded_config": {"limits.cpu": "2", "limits.cpu.allowance": "100ms/100ms", "limits.memory": "1GiB"},
            "expanded_devices": {"root": {"size": "4096MiB"}}}
    assert lxd.limits_match(inst, {"cpu_cores": 2, "cpu_allowance_pct": 50, "memory_bytes": GIB,
                                   "disk_bytes": 4 * GIB})
    assert not lxd.limits_match(inst, {"cpu_cores": 2, "cpu_allowance_pct": 100})
    assert not lxd.limits_match(inst, {"disk_bytes": 2 * GIB})
    assert not lxd.limits_match(inst, {})


# ------------------------------------------------------------ error mapping

def test_translate_outcomes():
    assert isinstance(lxd.translate(px.NotFound(Resp({"error": "not found"}, 404)), "x"), LxdNotFound)
    rej = lxd.translate(px.LXDAPIException(Resp({"error": "Invalid config"}, 400)), "x")
    assert isinstance(rej, LxdRejected) and "Invalid config" in rej.message
    never = requests.exceptions.ConnectionError(urllib3.exceptions.MaxRetryError(
        None, "/1.0", reason=urllib3.exceptions.ProtocolError("aborted", FileNotFoundError(2, "no socket"))))
    assert isinstance(lxd.translate(never, "x"), LxdUnavailable)
    assert isinstance(lxd.translate(px.ClientConnectionFailed("x"), "x"), LxdUnavailable)
    for exc in (requests.exceptions.ReadTimeout("slow"), requests.exceptions.ConnectionError("reset"),
                RuntimeError("?")):
        out = lxd.translate(exc, "x")
        assert isinstance(out, LxdUncertain) and not isinstance(out, LxdUnavailable)


# ------------------------------------------------------------ requests

def test_endpoint_is_explicit_unix_url(make_adapter, cfg):
    adapter, _ = make_adapter(lambda m, p, kw: Resp({"type": "sync", "metadata": []}))
    adapter.list_instances()
    assert adapter._seen[0]["endpoint"] == "http+unix://%2Fvar%2Fsnap%2Flxd%2Fcommon%2Flxd%2Funix.socket"
    assert adapter._seen[0]["timeout"] == cfg.lxd_timeout_seconds


def test_connect_failure_is_unavailable(cfg):
    def factory(**kw):
        raise px.ClientConnectionFailed("no socket")
    with pytest.raises(LxdUnavailable):
        lxd.LxdAdapter(cfg, client_factory=factory).find_by_marker("m")


def test_find_by_marker_uses_all_projects_and_local_config(make_adapter):
    listing = [raw_instance("a", marker="m1"), raw_instance("b", project="default", profile_marker="m2"),
               raw_instance("c", marker="dup"), raw_instance("d", project="other", marker="dup")]
    adapter, client = make_adapter(lambda m, p, kw: Resp({"type": "sync", "metadata": listing}))
    found = adapter.find_by_marker("m1")
    assert found["name"] == "a" and found["project"] == "hsm" and found["volatile_uuid"] == "uuid-a"
    assert client.calls[0] == ("GET", "/instances", {"params": {"recursion": 1, "all-projects": "true"}})
    assert adapter.find_by_marker("m2") is None          # a profile cannot confer identity
    with pytest.raises(AmbiguousIdentity):
        adapter.find_by_marker("dup")


def test_create_posts_template_and_waits(make_adapter):
    def handler(method, path, kw):
        if method == "POST":
            return async_resp()
        return wait_resp()
    adapter, client = make_adapter(handler)
    adapter.create("hsm", "web-1", **create_kwargs())
    post = client.calls[0]
    assert post[0] == "POST" and post[1] == "/instances" and post[2]["params"] == {"project": "hsm"}
    assert post[2]["json"]["config"]["security.privileged"] == "false"
    wait = client.calls[1]
    assert wait[1] == "/operations/op-1/wait" and wait[2]["params"] == {"timeout": lxd.CREATE_WAIT}
    assert wait[2]["timeout"] > lxd.CREATE_WAIT


def test_create_only_in_configured_project(make_adapter):
    adapter, client = make_adapter(lambda *a: async_resp())
    with pytest.raises(AdapterRefused):
        adapter.create("default", "web-1", **create_kwargs())
    assert client.calls == []


@pytest.mark.parametrize("wait,expected", [
    (lambda: wait_resp(400, "Failure", "image missing"), LxdRejected),
    (lambda: wait_resp(401, "Cancelled"), LxdRejected),
    (lambda: wait_resp(103, "Running"), LxdUncertain),
])
def test_operation_outcomes(make_adapter, wait, expected):
    adapter, _ = make_adapter(lambda m, p, kw: async_resp() if m == "PUT" else wait())
    with pytest.raises(expected) as info:
        adapter.start("hsm", "web-1")
    if expected is LxdUncertain:
        assert not isinstance(info.value, LxdRejected)


def test_lost_wait_is_uncertain_not_rejected(make_adapter):
    def handler(method, path, kw):
        if method == "PUT":
            return async_resp()
        raise px.NotFound(Resp({"error": "operation not found"}, 404))
    adapter, _ = make_adapter(handler)
    with pytest.raises(LxdUncertain):
        adapter.stop("hsm", "web-1", force=True)


def test_request_refused_up_front_is_rejected(make_adapter):
    def handler(method, path, kw):
        raise px.LXDAPIException(Resp({"error": "Instance is busy"}, 400))
    adapter, _ = make_adapter(handler)
    with pytest.raises(LxdRejected):
        adapter.freeze("hsm", "web-1")


def test_state_change_body(make_adapter):
    adapter, client = make_adapter(lambda m, p, kw: async_resp() if m == "PUT" else wait_resp())
    adapter.stop("hsm", "web-1", force=True)
    assert client.calls[0] == ("PUT", "/instances/web-1/state",
                               {"json": {"action": "stop", "timeout": lxd.STOP_TIMEOUT, "force": True},
                                "params": {"project": "hsm"}})


def test_names_are_validated_before_use(make_adapter):
    adapter, client = make_adapter(lambda *a: async_resp())
    for bad in ("../x", "a/b", "", "x?y"):
        with pytest.raises(AdapterRefused):
            adapter.delete("hsm", bad)
    with pytest.raises(AdapterRefused):
        adapter.delete("hsm/../x", "web-1")
    assert client.calls == []


def _limits_handler(root_size):
    def handler(method, path, kw):
        if method == "GET" and path == "/instances/web-1":
            return Resp({"type": "sync", "metadata": raw_instance("web-1", marker="m", root_size=root_size)})
        if method == "PATCH":
            return async_resp()
        return wait_resp()
    return handler


def test_disk_shrink_refused_before_lxd(make_adapter):
    adapter, client = make_adapter(_limits_handler("4096MiB"))
    with pytest.raises(AdapterRefused) as info:
        adapter.update_limits("hsm", "web-1", cpu_cores=1, cpu_allowance_pct=100, memory_bytes=GIB,
                              disk_bytes=2 * GIB)
    assert info.value.code == "DISK_SHRINK"
    assert [c[0] for c in client.calls] == ["GET"]


def test_update_limits_patch_shape(make_adapter):
    adapter, client = make_adapter(_limits_handler("2048MiB"))
    adapter.update_limits("hsm", "web-1", cpu_cores=2, cpu_allowance_pct=50, memory_bytes=2 * GIB,
                          disk_bytes=4 * GIB)
    patch = [c for c in client.calls if c[0] == "PATCH"][0]
    body = patch[2]["json"]
    assert body["config"]["limits.cpu.allowance"] == "100ms/100ms"
    assert body["config"]["limits.memory"] == "2048MiB"
    assert set(body["config"]) == {"limits.cpu", "limits.cpu.allowance", "limits.memory", "limits.memory.swap",
                                   "limits.processes"}
    assert body["devices"]["root"]["size"] == "4096MiB" and body["devices"]["root"]["pool"] == "hsm-btrfs"
    assert set(body["devices"]) == {"root", "eth0"}


def test_pool_free_bytes(make_adapter):
    adapter, client = make_adapter(lambda m, p, kw: Resp({"type": "sync", "metadata": {
        "space": {"total": 10 * GIB, "used": 3 * GIB}}}))
    assert adapter.pool_free_bytes("hsm-btrfs") == 7 * GIB
    assert client.calls[0][1] == "/storage-pools/hsm-btrfs/resources"
    with pytest.raises(AdapterRefused):
        adapter.pool_free_bytes("default")


def test_exec_command_validates_and_uses_config_bounds(make_adapter, cfg, monkeypatch):
    adapter, _ = make_adapter(lambda *a: None)
    for bad in ("", "a\x00b", "x" * (cfg.exec_max_command_bytes + 1)):
        with pytest.raises(AdapterRefused):
            adapter.exec_command("hsm", "web-1", bad, as_root=False)
    seen = {}

    def execute(argv, ident, out, err):
        seen.setdefault("argv", argv)
        out(b"ok")
        return 0
    monkeypatch.setattr(adapter, "_executor", lambda project, name: execute)
    res = adapter.exec_command("hsm", "web-1", "echo ok", as_root=False)
    assert seen["argv"][:4] == ["timeout", "-s", "KILL", str(cfg.exec_deadline_seconds)]
    assert res["stdout"] == "ok" and res["outcome"] == "completed"
