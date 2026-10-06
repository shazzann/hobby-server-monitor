import type { ContainerDetail, Me, Operation } from '../../lib/types';

/** Shared state between the container detail page modules. */
export interface ViewCtx {
  id: string;
  me: Me;
  isAdmin: boolean;
  /** Latest detail snapshot. */
  current(): ContainerDetail;
  /** Re-fetch the container now. */
  refresh(): void;
  /** Show and poll a lifecycle/limits/delete/owner operation. */
  track(op: Operation): void;
  /** Subscribe to detail updates. */
  onUpdate(fn: (c: ContainerDetail) => void): void;
}

export function opActive(c: ContainerDetail): boolean {
  const s = c.active_operation?.state;
  return s === 'queued' || s === 'running' || s === 'reconciling';
}
