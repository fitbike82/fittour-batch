import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { hostname } from 'node:os';
import { dirname, resolve } from 'node:path';

export class BatchError extends Error {
  constructor(public code: string) { super(code); }
}
export class QuotaStop extends BatchError {
  constructor() { super('daily_quota_exhausted'); }
}
export const kstDay = (date = new Date()) => new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
}).format(date);

export interface Budget { reserve(): void | Promise<void>; block(): void | Promise<void>; }
/** Serial CLI lock plus durable, conservative attempt accounting (including retries). */
export class DailyBudget implements Budget {
  private path: string;
  private lock: string;
  private state: Record<string, { calls: number; blocked?: boolean }>;
  constructor(key: string, private cap = 100_000, root = 'artifacts/tour-batch', alreadyUsed = 0) {
    if (!Number.isInteger(cap) || cap < 1 || cap > 100_000) throw new BatchError('invalid_daily_limit');
    const hash = createHash('sha256').update(key).digest('hex').slice(0, 24);
    this.path = resolve(root, `${hash}.json`);
    this.lock = resolve(root, `${hash}.lock`);
    mkdirSync(dirname(this.path), { recursive: true });
    if (existsSync(this.lock)) {
      const owner = JSON.parse(readFileSync(resolve(this.lock, 'owner.json'), 'utf8'));
      let alive = true;
      if (owner.host === hostname()) {
        try { process.kill(owner.pid, 0); } catch (error) {
          if ((error as NodeJS.ErrnoException).code === 'ESRCH') alive = false;
        }
      }
      if (alive) throw new BatchError('another_tour_batch_is_running');
      rmSync(this.lock, { recursive: true });
    }
    mkdirSync(this.lock); // Atomic acquisition; competing processes cannot both proceed.
    try {
      writeFileSync(resolve(this.lock, 'owner.json'), JSON.stringify({ pid: process.pid, host: hostname() }));
      this.state = existsSync(this.path) ? JSON.parse(readFileSync(this.path, 'utf8')) : {};
      for (const day of Object.values(this.state)) {
        if (!Number.isSafeInteger(day.calls) || day.calls < 0) throw new BatchError('invalid_quota_journal');
      }
      const day = this.current();
      day.calls = Math.max(day.calls, alreadyUsed);
      this.save();
    } catch (error) { this.close(); throw error; }
  }
  private current() { return this.state[kstDay()] ??= { calls: 0 }; }
  private save() {
    const temporary = `${this.path}.${process.pid}.tmp`;
    writeFileSync(temporary, JSON.stringify(this.state));
    renameSync(temporary, this.path);
  }
  reserve() {
    const day = this.current();
    if (day.blocked || day.calls >= this.cap) throw new QuotaStop();
    day.calls++;
    this.save(); // Persist BEFORE sending; a crash must never undercount a request.
  }
  block() { this.current().blocked = true; this.save(); }
  close() { rmSync(this.lock, { recursive: true, force: true }); }
}
