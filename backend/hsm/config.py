"""Environment-driven configuration shared by the API, worker and collector.

Every variable is documented in .env.example. Values are read once per
process; there is no runtime reconfiguration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
GIB = 1024 ** 3


def _load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader. Real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _path(name: str, default: Path) -> Path:
    """Relative paths are anchored at the repository root, not the process's cwd."""
    raw = _str(name)
    p = Path(raw).expanduser() if raw else default
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


def _list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(p.strip() for p in _str(name, default).split(",") if p.strip())


@dataclass(frozen=True)
class Config:
    public_base_url: str
    deployment_mode: str                 # 'local-http' | 'https'
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    session_secret: str
    session_absolute_hours: int
    session_idle_minutes: int
    invitation_ttl_hours: int
    bootstrap_admin_email: str
    data_dir: Path
    sqlite_path: Path
    metrics_dir: Path
    history_socket: Path
    history_allowed_uids: tuple[int, ...]
    dashboard_dist: Path
    lxd_socket: str
    lxd_project: str
    lxd_timeout_seconds: int
    allowed_pools: tuple[str, ...]
    allowed_networks: tuple[str, ...]
    image_alias_prefix: str
    host_cpu_reserve: int
    host_memory_reserve_bytes: int
    pool_reserve_bytes: int
    external_reserve_cpu_cores: int
    external_reserve_memory_bytes: int
    collect_interval_seconds: int
    raw_retention_hours: int
    rollup_retention_days: int
    metrics_max_bytes: int
    metrics_min_free_bytes: int
    history_max_points: int
    history_max_range_days: int
    exec_max_command_bytes: int
    exec_max_output_bytes: int
    exec_deadline_seconds: int
    exec_result_ttl_minutes: int
    container_max_processes: int
    audit_retention_days: int
    operation_retention_days: int
    backend_host: str
    backend_port: int
    trusted_proxy: bool
    extra: dict = field(default_factory=dict)

    @property
    def secure_cookies(self) -> bool:
        return self.deployment_mode == "https"

    @property
    def public_origin(self) -> str:
        u = urlparse(self.public_base_url)
        return f"{u.scheme}://{u.netloc}"


def load(env_file: str | os.PathLike | None = None) -> Config:
    _load_env_file(Path(env_file or os.environ.get("HSM_ENV_FILE", REPO_ROOT / ".env")))
    data_dir = _path("HSM_DATA_DIR", REPO_ROOT / "data")
    public_base_url = _str("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    mode = _str("HSM_DEPLOYMENT_MODE", "local-http")
    uid_default = str(os.getuid()) if hasattr(os, "getuid") else "0"
    cfg = Config(
        public_base_url=public_base_url,
        deployment_mode=mode,
        google_client_id=_str("GOOGLE_OAUTH_CLIENT_ID"),
        google_client_secret=_str("GOOGLE_OAUTH_CLIENT_SECRET"),
        google_redirect_uri=_str("GOOGLE_OAUTH_REDIRECT_URI", public_base_url + "/auth/google/callback"),
        session_secret=_str("SESSION_SECRET"),
        session_absolute_hours=_int("SESSION_ABSOLUTE_HOURS", 8),
        session_idle_minutes=_int("SESSION_IDLE_MINUTES", 30),
        invitation_ttl_hours=_int("INVITATION_TTL_HOURS", 72),
        bootstrap_admin_email=_str("BOOTSTRAP_ADMIN_EMAIL").lower(),
        data_dir=data_dir,
        sqlite_path=_path("SQLITE_DB_PATH", data_dir / "app.db"),
        metrics_dir=_path("TINYFLUX_DB_PATH", data_dir / "metrics"),
        history_socket=_path("HSM_HISTORY_SOCKET", data_dir / "history.sock"),
        history_allowed_uids=tuple(int(u) for u in _list("HSM_HISTORY_ALLOWED_UIDS", uid_default)),
        dashboard_dist=_path("HSM_DASHBOARD_DIST", REPO_ROOT / "dashboard" / "dist"),
        lxd_socket=_str("LXD_SOCKET", "/var/snap/lxd/common/lxd/unix.socket"),
        lxd_project=_str("LXD_PROJECT", "hsm"),
        lxd_timeout_seconds=_int("LXD_TIMEOUT_SECONDS", 10),
        allowed_pools=_list("HSM_ALLOWED_POOLS", "hsm-btrfs"),
        allowed_networks=_list("HSM_ALLOWED_NETWORKS", "hsmbr0"),
        image_alias_prefix=_str("HSM_IMAGE_ALIAS_PREFIX", "hsm/"),
        host_cpu_reserve=_int("HSM_HOST_CPU_RESERVE", 1),
        host_memory_reserve_bytes=_int("HSM_HOST_MEMORY_RESERVE_BYTES", 1 * GIB),
        pool_reserve_bytes=_int("HSM_POOL_RESERVE_BYTES", 2 * GIB),
        external_reserve_cpu_cores=_int("HSM_EXTERNAL_RESERVE_CPU_CORES", 0),
        external_reserve_memory_bytes=_int("HSM_EXTERNAL_RESERVE_MEMORY_BYTES", 0),
        collect_interval_seconds=_int("COLLECTOR_POLL_INTERVAL_SECONDS", 10),
        raw_retention_hours=_int("METRICS_RAW_RETENTION_HOURS", 6),
        rollup_retention_days=_int("METRICS_RETENTION_DAYS", 30),
        metrics_max_bytes=_int("METRICS_MAX_BYTES", 256 * 1024 * 1024),
        metrics_min_free_bytes=_int("METRICS_MIN_FREE_BYTES", 1 * GIB),
        history_max_points=min(_int("HISTORY_MAX_POINTS", 600), 600),
        history_max_range_days=_int("HISTORY_MAX_RANGE_DAYS", 31),
        exec_max_command_bytes=_int("EXEC_MAX_COMMAND_BYTES", 4096),
        exec_max_output_bytes=_int("EXEC_MAX_OUTPUT_BYTES", 65536),
        exec_deadline_seconds=_int("EXEC_DEADLINE_SECONDS", 20),
        exec_result_ttl_minutes=_int("EXEC_RESULT_TTL_MINUTES", 15),
        container_max_processes=_int("CONTAINER_MAX_PROCESSES", 500),
        audit_retention_days=_int("AUDIT_RETENTION_DAYS", 90),
        operation_retention_days=_int("OPERATION_RETENTION_DAYS", 14),
        backend_host=_str("BACKEND_HOST", "127.0.0.1"),
        backend_port=_int("BACKEND_PORT", 8000),
        trusted_proxy=_str("HSM_TRUSTED_PROXY", "false").lower() in ("1", "true", "yes"),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.deployment_mode not in ("local-http", "https"):
        raise ValueError("HSM_DEPLOYMENT_MODE must be 'local-http' or 'https'")
    host = urlparse(cfg.public_base_url).hostname or ""
    if cfg.deployment_mode == "local-http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("local-http mode is only allowed for a localhost PUBLIC_BASE_URL; use https")
    if cfg.deployment_mode == "https" and not cfg.public_base_url.startswith("https://"):
        raise ValueError("https mode requires an https:// PUBLIC_BASE_URL")
    if cfg.collect_interval_seconds < 1:
        raise ValueError("COLLECTOR_POLL_INTERVAL_SECONDS must be >= 1")
