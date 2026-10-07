// Types mirror docs/api-contract.md. Unknown values are `null`, never 0.

export type Role = 'admin' | 'user';

export interface ResourceTriple {
  cpu_cores: number | null;
  memory_bytes: number | null;
  disk_bytes: number | null;
}

export interface QuotaSummary {
  quota: ResourceTriple;
  allocated: ResourceTriple;
  pending: ResourceTriple;
  remaining: ResourceTriple;
}

export interface Me {
  user: { id: string; email: string; display_name: string | null; role: Role };
  csrf_token: string;
  quota: QuotaSummary | null;
}

export interface Limits {
  cpu_cores: number | null;
  cpu_allowance_pct?: number | null;
  memory_bytes: number | null;
  disk_bytes: number | null;
  pool: string | null;
}

export interface Metrics {
  state: string | null;
  cpu_pct: number | null;
  memory_bytes: number | null;
  memory_limit_bytes: number | null;
  disk_bytes: number | null;
  disk_limit_bytes: number | null;
  rx_bytes: number | null;
  tx_bytes: number | null;
  rx_rate: number | null;
  tx_rate: number | null;
  processes: number | null;
  ipv4: string | null;
  started_at: string | null;
  uptime_seconds: number | null;
  sampled_at: string | null;
  age_seconds: number | null;
  stale: boolean;
  quality: string[];
}

export type OperationState = 'queued' | 'running' | 'succeeded' | 'failed' | 'reconciling' | 'cancelled';

export interface ActiveOperation {
  id: string;
  kind: string;
  state: OperationState;
}

export interface ExecResult {
  outcome: 'completed' | 'timed_out' | 'unknown';
  exit_code: number | null;
  stdout: string;
  stderr: string;
  stdout_truncated: boolean;
  stderr_truncated: boolean;
  duration_ms: number | null;
  user: 'root' | 'hsm' | string;
}

export interface Operation {
  id: string;
  kind: string;
  state: OperationState;
  container_id: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: { code: string; message: string } | null;
  result: ExecResult | null;
}

export interface Container {
  id: string;
  name: string;
  project: string | null;
  instance_type: string | null;
  managed: boolean;
  status: string | null;
  safety: string | null;
  safety_reasons: string[];
  owner: { id: string; email: string } | null;
  limits: Limits | null;
  observed_limits: Limits | null;
  image_description: string | null;
  os: string | null;
  architecture: string | null;
  ephemeral: boolean | null;
  autostart: boolean | null;
  description: string | null;
  version: number;
  metrics: Metrics | null;
  active_operation: ActiveOperation | null;
}

export interface ContainerDetail extends Container {
  access?: { user_id: string; email: string }[];
  capabilities: { can_manage: boolean; can_exec: boolean; exec_as: 'root' | 'hsm' | null };
  /** Admin + managed only: live ranges with the current allocation counted as available. */
  limit_bounds?: CreationBounds;
}

export interface Collector {
  last_success_at: string | null;
  age_seconds: number | null;
  stale: boolean;
  lxd_available: boolean;
}

export interface ContainerList {
  containers: Container[];
  collector: Collector | null;
}

export type Range = '1h' | '6h' | '24h' | '7d' | '30d';
export type MetricName = 'cpu_pct' | 'memory_bytes' | 'disk_bytes' | 'rx_rate' | 'tx_rate' | 'processes';
export type Point = [number, number | null];

export interface History {
  container_id: string;
  start: number;
  end: number;
  resolution_seconds: number;
  source: 'raw' | 'rollup';
  series: Partial<Record<MetricName, { unit: string; points: Point[] }>>;
  coverage: number | null;
  gaps: [number, number][];
  available_from: number | null;
}

export interface Usage {
  start: number;
  end: number;
  covered_seconds: number | null;
  coverage: number | null;
  cpu_core_hours: number | null;
  memory_gib_hours: number | null;
  disk_avg_bytes: number | null;
  disk_max_bytes: number | null;
  rx_bytes: number | null;
  tx_bytes: number | null;
}

export interface Bound { min: number; max: number; step?: number }

export interface CreationBounds {
  cpu_cores: Bound;
  cpu_allowance_pct: Bound;
  memory_bytes: Bound;
  disk_bytes: Record<string, Bound>;
  blocked_reason: string | null;
}

export interface CreationOptions {
  options_version: string;
  images: { alias: string; fingerprint: string; description: string | null; os: string | null; release: string | null; architecture: string | null }[];
  pools: { name: string; driver: string; quota_capable: boolean; total_bytes: number | null; used_bytes: number | null }[];
  networks: { name: string }[];
  owners: { id: string; email: string }[];
  bounds: CreationBounds;
  name_rule: string;
  /** Quota summary of the selected owner (all values in bytes / cores). */
  owner_quota: { quota: ResourceTriple; allocated: ResourceTriple; pending: ResourceTriple; remaining: ResourceTriple } | null;
}

export interface UserRow {
  id: string;
  email: string;
  display_name: string | null;
  role: Role;
  status: 'pending' | 'active' | 'revoked' | string;
  quota: ResourceTriple;
  allocated: ResourceTriple;
  pending: ResourceTriple;
  invitation: { id: string; expires_at: string } | null;
  containers: { id: string; name: string; relation: 'owner' | 'assigned' }[];
}

export interface HostResource {
  total: number | null;
  reserve: number | null;
  budget: number | null;
  allocated: number | null;
  pending: number | null;
  remaining: number | null;
}

export interface PoolResource extends HostResource {
  name: string;
  driver: string | null;
  used: number | null;
}

export interface Accounting {
  host: { known: boolean; cpu: HostResource; memory: HostResource; pools: PoolResource[] };
  incomplete: unknown[];
  owners: { user_id: string; email: string; quota: ResourceTriple; allocated: ResourceTriple; pending: ResourceTriple; remaining: ResourceTriple }[];
}

export interface AuditEvent {
  id: number | string;
  at: string;
  actor_email: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  target_label: string | null;
  outcome: string;
  details: unknown;
}
