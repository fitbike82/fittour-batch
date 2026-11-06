-- Additive only. Existing master/detail/link/checkpoint tables remain authoritative.
CREATE TABLE IF NOT EXISTS collector_migrations (
 version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collector_runs (
 run_id TEXT PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL,
 ended_at TEXT, status TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS product_observations (
 id INTEGER PRIMARY KEY,
 event_key TEXT NOT NULL UNIQUE,
 product_id INTEGER REFERENCES affiliate_products(id),
 kind TEXT NOT NULL,
 source TEXT NOT NULL,
 observed_at TEXT,
 request_json TEXT NOT NULL DEFAULT '{}',
 payload_json TEXT NOT NULL,
 response_path TEXT,
 http_status INTEGER,
 status TEXT NOT NULL,
 error TEXT,
 run_id TEXT REFERENCES collector_runs(run_id),
 imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_product_kind ON product_observations(product_id,kind,observed_at);
CREATE INDEX IF NOT EXISTS observations_run ON product_observations(run_id);
CREATE TABLE IF NOT EXISTS product_prices (
 id INTEGER PRIMARY KEY,
 observation_id INTEGER NOT NULL REFERENCES product_observations(id),
 product_id INTEGER NOT NULL REFERENCES affiliate_products(id),
 json_path TEXT NOT NULL,
 value_json TEXT NOT NULL,
 currency TEXT,
 observed_at TEXT,
 request_json TEXT NOT NULL,
 source TEXT NOT NULL,
 UNIQUE(observation_id,json_path)
);
CREATE INDEX IF NOT EXISTS prices_product_time ON product_prices(product_id,observed_at);
CREATE TABLE IF NOT EXISTS product_locations (
 id INTEGER PRIMARY KEY,
 observation_id INTEGER NOT NULL REFERENCES product_observations(id),
 product_id INTEGER NOT NULL REFERENCES affiliate_products(id),
 json_path TEXT NOT NULL,
 place_name TEXT, country TEXT, city TEXT, address TEXT,
 latitude_json TEXT, longitude_json TEXT,
 source TEXT NOT NULL, verification_status TEXT NOT NULL DEFAULT 'PROVIDER_REPORTED',
 payload_json TEXT NOT NULL,
 UNIQUE(observation_id,json_path)
);
CREATE INDEX IF NOT EXISTS locations_product ON product_locations(product_id);
CREATE VIEW IF NOT EXISTS collector_link_state AS
 SELECT p.id AS product_id,
 EXISTS(SELECT 1 FROM affiliate_links l WHERE l.affiliate_company_id=p.affiliate_company_id
 AND l.target_url=p.product_url AND (l.affiliate_product_id=p.id OR l.affiliate_product_id IS NULL)) AS has_link,
 COALESCE(o.status,r.status) AS last_attempt_status,
 CASE WHEN o.id IS NOT NULL THEN o.error ELSE r.error END AS last_attempt_error,
 COALESCE(o.observed_at,r.updated_at) AS last_attempt_at
 FROM affiliate_products p
 LEFT JOIN enrichment_runs r ON r.product_id=p.id AND r.kind='link'
 LEFT JOIN product_observations o ON o.id=(SELECT a.id FROM product_observations a
 WHERE a.product_id=p.id AND a.kind='link' AND a.source!='existing_link'
 ORDER BY a.observed_at DESC,a.id DESC LIMIT 1);
