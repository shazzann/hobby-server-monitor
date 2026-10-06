-- Hobby Server Monitor: initial schema.
-- Conventions: application ids are UUIDv4 text; timestamps are UTC ISO-8601
-- text ('YYYY-MM-DDTHH:MM:SSZ'); resource quantities are integers
-- (bytes, whole CPU cores, allowance percent).

CREATE TABLE users (
    id                  TEXT PRIMARY KEY,
    google_sub          TEXT UNIQUE,                 -- bound on first admission
    email               TEXT NOT NULL UNIQUE,        -- canonical (trimmed, lower-cased)
    display_name        TEXT NOT NULL DEFAULT '',
    role                TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    status              TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked')),
    quota_cpu_cores     INTEGER NOT NULL DEFAULT 0 CHECK (quota_cpu_cores >= 0),
    quota_memory_bytes  INTEGER NOT NULL DEFAULT 0 CHECK (quota_memory_bytes >= 0),
    quota_disk_bytes    INTEGER NOT NULL DEFAULT 0 CHECK (quota_disk_bytes >= 0),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    revoked_at          TEXT
);

CREATE TABLE invitations (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    email        TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,              -- sha256 of the one-time secret
    invited_by   TEXT NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    accepted_at  TEXT,
    revoked_at   TEXT
);
-- At most one open invitation per email.
CREATE UNIQUE INDEX invitations_one_open ON invitations(email)
    WHERE accepted_at IS NULL AND revoked_at IS NULL;

CREATE TABLE sessions (
    id                TEXT PRIMARY KEY,
    token_hash        TEXT NOT NULL UNIQUE,         -- sha256 of the cookie value
    user_id           TEXT NOT NULL REFERENCES users(id),
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,                -- absolute expiry
    last_seen_at      TEXT NOT NULL,                -- idle expiry is derived from this
    revoked_at        TEXT
);
CREATE INDEX sessions_user_active ON sessions(user_id) WHERE revoked_at IS NULL;

CREATE TABLE oauth_transactions (
    id                  TEXT PRIMARY KEY,
    state_hash          TEXT NOT NULL UNIQUE,
    browser_hash        TEXT NOT NULL,              -- sha256 of the HttpOnly binding cookie
    nonce               TEXT NOT NULL,
    code_verifier       TEXT NOT NULL,              -- PKCE
    admission_kind      TEXT CHECK (admission_kind IN ('invitation', 'bootstrap')),
    admission_ref       TEXT,                       -- invitation id, or token hash for bootstrap
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    consumed_at         TEXT
);

CREATE TABLE containers (
    id                   TEXT PRIMARY KEY,          -- stable application id
    project              TEXT NOT NULL,
    name                 TEXT NOT NULL,             -- current LXD name; a label, never an auth key
    instance_type        TEXT NOT NULL DEFAULT 'container'
                         CHECK (instance_type IN ('container', 'virtual-machine')),
    lxd_marker           TEXT,                      -- value of user.hsm.id; NULL = unmanaged
    lxd_volatile_uuid    TEXT,                      -- volatile.uuid at adoption/creation
    managed              INTEGER NOT NULL DEFAULT 0 CHECK (managed IN (0, 1)),
    status               TEXT NOT NULL CHECK (status IN
                         ('creating', 'active', 'quarantined', 'failed', 'deleted')),
    safety               TEXT NOT NULL DEFAULT 'unknown' CHECK (safety IN ('safe', 'unsafe', 'unknown')),
    safety_reasons       TEXT NOT NULL DEFAULT '[]', -- JSON list of strings
    owner_id             TEXT REFERENCES users(id),
    -- committed accounting state (written only by worker/admin transactions)
    cpu_cores            INTEGER CHECK (cpu_cores IS NULL OR cpu_cores > 0),
    cpu_allowance_pct    INTEGER CHECK (cpu_allowance_pct IS NULL OR cpu_allowance_pct BETWEEN 1 AND 100),
    memory_bytes         INTEGER CHECK (memory_bytes IS NULL OR memory_bytes > 0),
    disk_bytes           INTEGER CHECK (disk_bytes IS NULL OR disk_bytes > 0),
    pool                 TEXT,
    -- observed LXD configuration (written only by the collector)
    observed_cpu_cores   INTEGER,                   -- NULL = unlimited / unknown
    observed_memory_bytes INTEGER,
    observed_disk_bytes  INTEGER,
    observed_pool        TEXT,
    image_description    TEXT NOT NULL DEFAULT '',
    os                   TEXT NOT NULL DEFAULT '',
    architecture         TEXT NOT NULL DEFAULT '',
    ephemeral            INTEGER NOT NULL DEFAULT 0,
    autostart            INTEGER NOT NULL DEFAULT 0,
    description          TEXT NOT NULL DEFAULT '',
    version              INTEGER NOT NULL DEFAULT 1, -- bumps on committed changes
    created_at           TEXT NOT NULL,
    last_seen_at         TEXT,
    deleted_at           TEXT
);
CREATE UNIQUE INDEX containers_live_name ON containers(project, name) WHERE status != 'deleted';
CREATE UNIQUE INDEX containers_live_marker ON containers(lxd_marker)
    WHERE lxd_marker IS NOT NULL AND status != 'deleted';
CREATE INDEX containers_owner ON containers(owner_id) WHERE status != 'deleted';

CREATE TABLE container_access (
    container_id  TEXT NOT NULL REFERENCES containers(id),
    user_id       TEXT NOT NULL REFERENCES users(id),
    granted_by    TEXT REFERENCES users(id),
    granted_at    TEXT NOT NULL,
    PRIMARY KEY (container_id, user_id)
);
CREATE INDEX container_access_user ON container_access(user_id);

CREATE TABLE operations (
    id               TEXT PRIMARY KEY,
    actor_id         TEXT NOT NULL REFERENCES users(id),
    container_id     TEXT REFERENCES containers(id),
    kind             TEXT NOT NULL CHECK (kind IN ('create', 'start', 'stop', 'restart', 'freeze',
                     'unfreeze', 'update_limits', 'delete', 'exec', 'adopt')),
    payload          TEXT NOT NULL DEFAULT '{}',    -- typed JSON; exec command is cleared when finished
    request_hash     TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    state            TEXT NOT NULL CHECK (state IN
                     ('queued', 'running', 'succeeded', 'failed', 'reconciling', 'cancelled')),
    lease_owner      TEXT,
    lease_expires_at TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    error_code       TEXT,
    error_message    TEXT,
    result           TEXT,                          -- JSON; exec output, expires
    result_expires_at TEXT,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    UNIQUE (actor_id, idempotency_key)
);
CREATE INDEX operations_queue ON operations(state, created_at)
    WHERE state IN ('queued', 'running', 'reconciling');
CREATE INDEX operations_container_active ON operations(container_id)
    WHERE state IN ('queued', 'running', 'reconciling');

CREATE TABLE quota_reservations (
    operation_id   TEXT PRIMARY KEY REFERENCES operations(id),
    owner_id       TEXT NOT NULL REFERENCES users(id),
    pool           TEXT,
    cpu_cores      INTEGER NOT NULL DEFAULT 0 CHECK (cpu_cores >= 0),
    memory_bytes   INTEGER NOT NULL DEFAULT 0 CHECK (memory_bytes >= 0),
    disk_bytes     INTEGER NOT NULL DEFAULT 0 CHECK (disk_bytes >= 0),
    state          TEXT NOT NULL CHECK (state IN ('pending', 'released')),
    created_at     TEXT NOT NULL,
    released_at    TEXT
);
CREATE INDEX quota_reservations_pending ON quota_reservations(owner_id) WHERE state = 'pending';

CREATE TABLE latest_metrics (
    container_id     TEXT PRIMARY KEY REFERENCES containers(id),
    state            TEXT NOT NULL,                 -- Running/Stopped/Frozen/Error/Unknown
    cpu_pct          REAL,                          -- % of allocated cores; NULL = unknown
    memory_bytes     INTEGER,
    memory_limit_bytes INTEGER,
    disk_bytes       INTEGER,
    disk_limit_bytes INTEGER,
    rx_bytes         INTEGER,                       -- cumulative counters
    tx_bytes         INTEGER,
    rx_rate          REAL,                          -- bytes/second; NULL = no valid interval
    tx_rate          REAL,
    processes        INTEGER,
    ipv4             TEXT,
    started_at       TEXT,                          -- verified start time, NULL if unknown
    sampled_at       TEXT NOT NULL,
    quality          TEXT NOT NULL DEFAULT '[]'     -- JSON list of flags
);

CREATE TABLE audit_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    actor_id      TEXT,
    actor_email   TEXT,                             -- snapshot; 'system' for collector/worker
    action        TEXT NOT NULL,
    target_type   TEXT,
    target_id     TEXT,
    target_label  TEXT,                             -- snapshot (container name / email)
    outcome       TEXT NOT NULL CHECK (outcome IN ('requested', 'succeeded', 'failed', 'denied')),
    details       TEXT NOT NULL DEFAULT '{}',       -- JSON: before/after limits etc. Never secrets or command output.
    request_id    TEXT,
    operation_id  TEXT
);
CREATE INDEX audit_events_at ON audit_events(at);

CREATE TABLE service_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,                      -- JSON
    updated_at  TEXT NOT NULL
);

CREATE TABLE rate_limits (
    bucket      TEXT PRIMARY KEY,
    window_start TEXT NOT NULL,
    count       INTEGER NOT NULL
);
