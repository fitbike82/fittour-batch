-- Matches claim_batch_tour_places_filtered: category equality, content_id keyset/order.
-- Completed/inactive rows leave this index as the batch drains.
-- The existing places_fast_enrichment_queue_idx(content_id) remains useful for
-- unfiltered/multiple-category claims ordered globally by content_id.
-- Do not predicate on now(): pending leases must become eligible as time passes.
begin;
create index if not exists places_enrich_claim_category_id_idx
  on public.places(content_type_id, content_id)
  where is_active and enrich_status is distinct from 'completed'
    and content_type_id in ('12','14','15','25','28','32','38','39');
analyze public.places;
commit;
