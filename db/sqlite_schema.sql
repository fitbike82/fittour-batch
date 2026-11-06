-- Existing schema preserved; use repository migration for existing databases.
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS affiliate_companies (
       id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
       integration_type TEXT NOT NULL DEFAULT 'API', api_base_url TEXT, is_active INTEGER NOT NULL DEFAULT 1
    );
CREATE TABLE IF NOT EXISTS affiliate_products (
       id INTEGER PRIMARY KEY, affiliate_company_id INTEGER NOT NULL REFERENCES affiliate_companies(id),
       provider_product_id TEXT NOT NULL, product_type TEXT NOT NULL, title TEXT, product_url TEXT,
       source_keyword TEXT, source_city TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
       raw_json TEXT NOT NULL, UNIQUE(affiliate_company_id,provider_product_id,product_type)
    );
CREATE TABLE IF NOT EXISTS affiliate_links (
       id INTEGER PRIMARY KEY, affiliate_company_id INTEGER NOT NULL REFERENCES affiliate_companies(id),
       affiliate_product_id INTEGER REFERENCES affiliate_products(id), target_url TEXT NOT NULL,
       affiliate_url TEXT NOT NULL, placement TEXT NOT NULL DEFAULT 'default', created_at TEXT NOT NULL,
       UNIQUE(affiliate_company_id,target_url,placement)
    );
CREATE TABLE IF NOT EXISTS api_probe_runs (
       id INTEGER PRIMARY KEY, endpoint TEXT NOT NULL, keyword TEXT, city TEXT,
       status_code INTEGER NOT NULL, elapsed_ms INTEGER, result_count INTEGER,
       collection_path TEXT, fields_json TEXT, response_path TEXT, recorded_at TEXT NOT NULL
    );
CREATE TABLE IF NOT EXISTS collection_jobs (
      job_key TEXT PRIMARY KEY, job_json TEXT NOT NULL, next_page INTEGER NOT NULL,
      status TEXT NOT NULL, last_error TEXT, total_count INTEGER, pages_done INTEGER NOT NULL DEFAULT 0,
      fetched_items INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
    );
CREATE TABLE IF NOT EXISTS collection_pages (
      job_key TEXT NOT NULL REFERENCES collection_jobs(job_key), requested_page INTEGER NOT NULL,
      returned_page INTEGER, http_status INTEGER NOT NULL, item_count INTEGER NOT NULL,
      distinct_item_count INTEGER NOT NULL, total_count INTEGER, overlapping_ids INTEGER NOT NULL,
      response_file TEXT, fetched_at TEXT NOT NULL,
      PRIMARY KEY(job_key,requested_page)
    );
CREATE TABLE IF NOT EXISTS collection_seen_ids (
      job_key TEXT NOT NULL REFERENCES collection_jobs(job_key), product_id TEXT NOT NULL,
      first_page INTEGER NOT NULL, PRIMARY KEY(job_key, product_id)
    );
CREATE TABLE IF NOT EXISTS collection_overlap_events (
      job_key TEXT NOT NULL, requested_page INTEGER NOT NULL, product_id TEXT NOT NULL, first_page INTEGER NOT NULL,
      detected_at TEXT NOT NULL, PRIMARY KEY(job_key,requested_page,product_id));
CREATE TABLE IF NOT EXISTS stay44_sessions (
      session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
      status TEXT NOT NULL, jobs_count INTEGER NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
      note TEXT NOT NULL DEFAULT 'OBSERVED_ONLY_NOT_EXHAUSTIVE');
CREATE TABLE IF NOT EXISTS stay44_observations (
      session_id TEXT NOT NULL, job_key TEXT NOT NULL, keyword TEXT NOT NULL,
      checkin TEXT NOT NULL, checkout TEXT NOT NULL, adults INTEGER NOT NULL,
      round_number INTEGER NOT NULL, requested_page INTEGER NOT NULL,
      requested_at TEXT NOT NULL, status_code INTEGER NOT NULL,
      reported_total INTEGER, returned_page INTEGER,
      ids_json TEXT NOT NULL, raw_response_path TEXT,
      count INTEGER NOT NULL, new_in_job INTEGER NOT NULL,
      overlap_in_job INTEGER NOT NULL, same_page_jaccard REAL,
      error TEXT, PRIMARY KEY(session_id,job_key,round_number,requested_page));
CREATE TABLE IF NOT EXISTS nationwide_jobs(
      job_id TEXT PRIMARY KEY, phase TEXT NOT NULL, region_json TEXT NOT NULL,
      keyword TEXT NOT NULL, checkin TEXT, checkout TEXT, adults INTEGER,
      next_page INTEGER NOT NULL, status TEXT NOT NULL, error TEXT,
      pages INTEGER NOT NULL DEFAULT 0, returned INTEGER NOT NULL DEFAULT 0,
      unique_seen INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nationwide_pages(
      job_id TEXT NOT NULL, page INTEGER NOT NULL, response_path TEXT,
      http_status INTEGER NOT NULL, returned INTEGER NOT NULL,
      new_ids INTEGER NOT NULL, overlap_ids INTEGER NOT NULL,
      reported_total INTEGER, has_next TEXT, fetched_at TEXT NOT NULL,
      PRIMARY KEY(job_id,page));
CREATE TABLE IF NOT EXISTS nationwide_seen(
      job_id TEXT NOT NULL, provider_id TEXT NOT NULL,
      PRIMARY KEY(job_id,provider_id));
CREATE TABLE IF NOT EXISTS nationwide_product_observations(
      phase TEXT NOT NULL, provider_id TEXT NOT NULL, job_id TEXT NOT NULL,
      source_region_json TEXT NOT NULL, query_keyword TEXT NOT NULL,
      observed_at TEXT NOT NULL,
      PRIMARY KEY(phase,provider_id,job_id));
CREATE TABLE IF NOT EXISTS enrichment_runs (
      product_id INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
      attempts INTEGER NOT NULL DEFAULT 0, requested_at TEXT, updated_at TEXT NOT NULL,
      response_path TEXT, error TEXT, PRIMARY KEY(product_id,kind));
CREATE TABLE IF NOT EXISTS enrichment_details (
      product_id INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
      payload_json TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(product_id,kind));
CREATE INDEX IF NOT EXISTS stay44_obs_job ON stay44_observations(job_key,requested_page,requested_at);
CREATE INDEX IF NOT EXISTS idx_enrichment_status ON enrichment_runs(kind,status);

INSERT OR IGNORE INTO affiliate_companies(id,code,name,api_base_url)
 VALUES(1,'MYREALTRIP','마이리얼트립','https://partner-ext-api.myrealtrip.com');
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
