import path from 'node:path';
import { parseArgs } from 'node:util';
import { config } from 'dotenv';
import pLimit from 'p-limit';
import {
  collectPlaceSources,
  createKakaoSearch,
  createNaverSearch,
  hasNonEmptyCollectionFile,
  loadSearchCredentials,
  readPlacesCsv,
  saveCollectionFile,
  toCollectionFile,
  withRequestDelay,
} from '../src/lib/local-review-collector.js';

function nonNegativeInteger(value: string, name: string): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) throw new Error(`${name} must be a non-negative integer`);
  return parsed;
}

async function main(): Promise<void> {
  const { values } = parseArgs({ options: {
    'env-file': { type: 'string', default: '.env' },
    'input-file': { type: 'string', default: 'docs/관광지_list_1차.csv' },
    'output-dir': { type: 'string', default: 'data/raw_sources' },
    limit: { type: 'string' },
    offset: { type: 'string', default: '0' },
    concurrency: { type: 'string', default: '5' },
  } });
  const loaded = config({ path: values['env-file'], quiet: true });
  if (loaded.error) throw new Error(`cannot load env file: ${values['env-file']}`);

  const offset = nonNegativeInteger(values.offset, 'offset');
  const runLimit = values.limit === undefined ? undefined : nonNegativeInteger(values.limit, 'limit');
  const concurrency = nonNegativeInteger(values.concurrency, 'concurrency');
  if (concurrency < 1 || concurrency > 20) throw new Error('concurrency must be an integer from 1 to 20');

  const inputFile = path.resolve(values['input-file']);
  const outputDirectory = path.resolve(values['output-dir']);
  const allPlaces = await readPlacesCsv(inputFile);
  const places = allPlaces.slice(offset, runLimit === undefined ? undefined : offset + runLimit);
  const credentials = loadSearchCredentials();
  const naverSearch = withRequestDelay(createNaverSearch(credentials.naverClientId, credentials.naverClientSecret));
  const kakaoSearch = withRequestDelay(createKakaoSearch(credentials.kakaoApiKey));
  const parallel = pLimit(concurrency);
  let completed = 0;
  let skipped = 0;

  await Promise.all(places.map((place, index) => parallel(async () => {
    if (await hasNonEmptyCollectionFile(outputDirectory, place.content_id)) {
      skipped++;
      console.log(`[${index + 1}/${places.length}] SKIP ${place.content_id} ${place.title}`);
      return;
    }
    const sources = await collectPlaceSources(place, naverSearch, kakaoSearch);
    await saveCollectionFile(outputDirectory, toCollectionFile(place, sources));
    completed++;
    console.log(`[${index + 1}/${places.length}] SAVE ${place.content_id} ${place.title}: ${sources.length} sources`);
  })));
  console.log(`Done: saved=${completed}, skipped=${skipped}, selected=${places.length}, CSV rows=${allPlaces.length}`);
}

await main().catch(error => {
  console.error(error instanceof Error ? error.message : 'local review source collection failed');
  process.exitCode = 1;
});
