import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import test from 'node:test';
import { parse } from 'yaml';

test('every workflow job notifies Telegram even when setup or tests fail', () => {
  for (const file of readdirSync('.github/workflows').filter(name => name.endsWith('.yml'))) {
    const workflow = parse(readFileSync(`.github/workflows/${file}`, 'utf8'));
    for (const job of Object.values(workflow.jobs) as any[]) {
      const notify = job.steps.at(-1);
      assert.equal(notify.name, 'Notify Telegram of workflow outcome', file);
      assert.equal(notify.if, 'always()', file);
      assert.equal(notify.env.JOB_STATUS, '${{ job.status }}', file);
      assert.equal(notify.env.TELEGRAM_BOT_TOKEN, '${{ secrets.TELEGRAM_BOT_TOKEN }}', file);
      assert.equal(notify.env.TELEGRAM_CHAT_ID, '${{ secrets.TELEGRAM_CHAT_ID }}', file);
      assert.match(notify.run, /🚨 \[FitTour\] 배치 실패 경보:.*단계 에러 발생/, file);
      assert.match(notify.run, /--fail-with-body --retry 3/, file);
      assert.match(notify.run, /\.ok == true/, file);
      assert.ok(job.steps.slice(0, -1).every((step: any) => step.id), file);
    }
  }
});
