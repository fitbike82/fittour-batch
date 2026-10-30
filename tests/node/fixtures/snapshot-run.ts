import { cli } from '../../../src/lib/cli.js';
import { enrich } from '../../../src/jobs/enrichment.js';
import { BatchError } from '../../../src/lib/budget.js';

// Exercise the real CLI/report writer and SupabaseStore without external services.
let snapshots = 0, next = 1, saved = 0;
const limit = Number(process.env.TEST_PLACE_LIMIT);
globalThis.fetch = async (input, init) => {
  const url = String(input);
  const rpc = url.split('/').at(-1);
  let data: unknown;
  if (rpc === 'batch_tour_relay_preflight') data = true;
  else if (rpc === 'batch_tour_snapshot') {
    snapshots++;
    if (process.env.TEST_SNAPSHOT_ERROR === 'network') throw new TypeError('private network details');
    return new Response(JSON.stringify({ code: '57014', message: 'query canceled' }), {
      status: 500, headers: { 'Content-Type': 'application/json' },
    });
  } else if (rpc === 'claim_batch_tour_places_filtered') {
    const params = JSON.parse(String(init?.body));
    data = Array.from({ length: Math.min(params.p_limit, limit - next + 1) }, () => {
      const id = String(next++);
      return { content_id: id, content_type_id: '12', title: 'Place', claim_token: id };
    });
  } else if (rpc === 'complete_batch_tour_place') { saved++; data = true; }
  else throw new Error(`Unexpected RPC: ${rpc}`);
  return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
};

await cli(async (store, api, opt) => {
  api.page = async (_endpoint, params) => {
    api.calls++;
    return { items: [{ contentid: params.contentId }], total: 1 };
  };
  const result = await enrich(store, api, opt, event => console.log(JSON.stringify(event)));
  if (process.env.TEST_JOB_RESULT === 'fatal') throw new BatchError('database_claim_08006');
  return { ...result, deferred: process.env.TEST_JOB_RESULT === 'deferred', snapshot_calls_during_job: snapshots };
});
console.log(JSON.stringify({ event: 'test_totals', snapshots, saved }));
