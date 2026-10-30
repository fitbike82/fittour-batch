begin;
alter table public.places add column if not exists enrichment_error text;

create or replace function public.fail_batch_tour_place(
  p_content_id text, p_token uuid, p_failed boolean, p_error text
) returns boolean language plpgsql security invoker set search_path='' as $$
begin
  update public.places set enrich_status='failed', enrichment_error=left(p_error,200),
    enrich_claim_token=null, enrich_claim_until=null
  where content_id=p_content_id and enrich_claim_token=p_token and enrich_status='pending'
    and p_failed;
  return found;
end;
$$;
revoke all on function public.fail_batch_tour_place(text,uuid,boolean,text) from public,anon,authenticated;
grant execute on function public.fail_batch_tour_place(text,uuid,boolean,text) to service_role;

-- Clear a previous failure code when a retry successfully commits.
create or replace function batch_tour.clear_enrichment_error()
returns trigger language plpgsql security invoker set search_path='' as $$
begin
  if new.enrich_status='completed' then new.enrichment_error := null; end if;
  return new;
end;
$$;
revoke all on function batch_tour.clear_enrichment_error() from public,anon,authenticated;
grant execute on function batch_tour.clear_enrichment_error() to service_role;
drop trigger if exists clear_enrichment_error on public.places;
create trigger clear_enrichment_error before update of enrich_status on public.places
  for each row execute function batch_tour.clear_enrichment_error();
commit;
