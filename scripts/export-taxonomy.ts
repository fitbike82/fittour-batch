import { readFileSync, writeFileSync } from 'node:fs';
import { exportTaxonomy } from '../src/lib/taxonomy.js';
const path = new URL('./place_taxonomy.json', import.meta.url);
const generated = JSON.stringify(exportTaxonomy(), null, 2) + '\n';
if (process.argv.includes('--check')) {
  if (readFileSync(path, 'utf8').replace(/\r\n/g, '\n') !== generated) {
    throw new Error('Taxonomy bridge is stale. Run npm run taxonomy:sync');
  }
} else writeFileSync(path, generated, 'utf8');
