import { readdir } from 'node:fs/promises';
import path from 'node:path';
import { parseArgs } from 'node:util';
import {
  hasValidFullText,
  readRawSourceFile,
  saveRawSourceFile,
  scrapeSource,
} from '../src/lib/local-review-scraper.js';

function nonNegativeInteger(value: string, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) throw new Error(`${name} must be a non-negative integer`);
  return parsed;
}

async function main(): Promise<void> {
  const { values } = parseArgs({ options: {
    'input-dir': { type: 'string', default: 'data/raw_sources' },
    limit: { type: 'string' },
  } });
  const limit = values.limit === undefined ? undefined : nonNegativeInteger(values.limit, 'limit');
  const inputDirectory = path.resolve(values['input-dir']);
  const names = (await readdir(inputDirectory))
    .filter(name => name.toLowerCase().endsWith('.json'))
    .sort((left, right) => left.localeCompare(right, 'en', { numeric: true }));
  const selected = names.slice(0, limit);
  let scraped = 0, failed = 0, skipped = 0, fileErrors = 0, attempted = 0;

  for (const [fileIndex, name] of selected.entries()) {
    const filePath = path.join(inputDirectory, name);
    try {
      const payload = await readRawSourceFile(filePath);
      for (let sourceIndex = 0; sourceIndex < payload.sources.length; sourceIndex++) {
        const source = payload.sources[sourceIndex];
        if (hasValidFullText(source)) { skipped++; continue; }
        if (attempted > 0) {
          const delay = 300 + Math.floor(Math.random() * 201);
          await new Promise(resolve => setTimeout(resolve, delay));
        }
        attempted++;
        const updated = await scrapeSource(source);
        payload.sources[sourceIndex] = updated;
        if (updated.scrape_status === 'scraped') scraped++; else failed++;
        await saveRawSourceFile(filePath, payload);
      }
      console.log(`[${fileIndex + 1}/${selected.length}] ${name}: sources=${payload.sources.length}`);
    } catch (error) {
      fileErrors++;
      console.error(`[${fileIndex + 1}/${selected.length}] ${name}: ${error instanceof Error ? error.message : 'file failed'}`);
    }
  }
  console.log(`Done: files=${selected.length}, scraped=${scraped}, failed=${failed}, skipped=${skipped}, file_errors=${fileErrors}`);
  if (fileErrors) process.exitCode = 1;
}

await main().catch(error => {
  console.error(error instanceof Error ? error.message : 'local review body scraping failed');
  process.exitCode = 1;
});
