-- Parameterized reference statements, for the future PostgreSQL adapter.
-- Not a script to run literally. Bind parameters; never interpolate API data.
-- All work uses private schema mrt_collector and explicit transactions.

-- Product natural key UPSERT (keep first_seen_at and stable internal id):
-- INSERT INTO mrt_collector.affiliate_products
-- (affiliate_company_id,provider_product_id,product_type,title,product_url,
--  source_keyword,source_city,first_seen_at,last_seen_at,raw_json,raw_json_text)
-- VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
-- ON CONFLICT (affiliate_company_id,provider_product_id,product_type)
-- DO UPDATE SET title=EXCLUDED.title,
-- product_url=COALESCE(EXCLUDED.product_url,affiliate_products.product_url),
-- last_seen_at=EXCLUDED.last_seen_at,raw_json=EXCLUDED.raw_json,
-- raw_json_text=EXCLUDED.raw_json_text
-- RETURNING id;

-- Observation: INSERT ... ON CONFLICT (event_key) DO NOTHING RETURNING id;
-- If no row is returned, SELECT id WHERE event_key=$1. Never overwrite evidence.
-- Prices/locations: ON CONFLICT (observation_id,json_path) DO NOTHING.
-- Enrichment latest cache: ON CONFLICT (product_id,kind) DO UPDATE only on success.
-- Links: look up matching provider/product/target URL before an API request,
-- regardless of placement. Keep issued affiliate_url unchanged on request errors.
-- Checkpoints: update in the same transaction as accepted page observations.
-- Runs: generate a new run_id per execution. Do not replace old runs.

-- After first import with explicit IDs, for every identity id table:
-- SELECT setval(pg_get_serial_sequence('mrt_collector.affiliate_products','id'),
--               COALESCE((SELECT MAX(id) FROM mrt_collector.affiliate_products),1),
--               EXISTS(SELECT 1 FROM mrt_collector.affiliate_products));
