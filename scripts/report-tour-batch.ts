import { BatchError } from '../src/lib/budget.js';
import { configuredStore, options } from '../src/lib/cli.js';
import { saveReport } from '../src/lib/report.js';
try {
  const opt = options();
  if (!opt.help) {
    const { store } = configuredStore(opt);
    const snapshot = await store.snapshot();
    console.log(JSON.stringify(snapshot, null, 2));
    saveReport({ job: 'population', started_at: new Date().toISOString(), status: 'completed',
      before: snapshot, after: snapshot }, opt.report);
  }
} catch (error) {
  console.error(error instanceof BatchError ? error.code : 'report_failed');
  process.exitCode = 1;
}
