import { BatchError } from './budget.js';
import type { Snapshot, Store } from './store.js';

// Statistics are optional: never let their failure replace the collection result.
export async function optionalSnapshot(store: Pick<Store, 'snapshot'>, phase: 'before' | 'after',
  log: (event: object) => void = event => console.log(JSON.stringify(event))): Promise<Snapshot | undefined> {
  if (!store.snapshot) return undefined;
  try {
    const database = await store.snapshot();
    log({ event: 'batch_snapshot', phase, database });
    return database;
  } catch (error) {
    log({ event: 'snapshot_skipped', phase,
      reason: error instanceof BatchError ? error.code : error instanceof Error ? error.name : 'snapshot_unavailable' });
    return undefined;
  }
}
