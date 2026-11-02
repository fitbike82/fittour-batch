-- Apply on the Tour batch database before deploying the taxonomy-enabled worker.
begin;
alter table public.places
  add column if not exists is_century_store boolean not null default false,
  add column if not exists century_store_info jsonb default null,
  add column if not exists category_l1 text default null,
  add column if not exists category_l2 text default null,
  add column if not exists filter_tags text[] default '{}',
  add column if not exists taxonomy_source text default null;
create index if not exists idx_places_filter_tags on public.places using gin(filter_tags);
create index if not exists idx_places_category_l1_l2 on public.places(category_l1,category_l2);
create index if not exists idx_places_century_flag on public.places(is_century_store) where is_century_store=true;

-- Invoker-only and service-role-only. Call while the review completion holds
-- its place/revision/lease locks; standalone legacy calls still check revision.
create or replace function public.apply_ai_place_taxonomy(
  p_content_id text,p_modified_time text,p_taxonomy jsonb
) returns boolean language plpgsql security invoker set search_path='' as $$
declare expected text; affected integer;
begin
  select case p.content_type_id::text when '12' then '관광지' when '14' then '문화시설'
    when '15' then '축제/행사' when '25' then '여행코스' when '28' then '레포츠'
    when '32' then '숙박' when '38' then '쇼핑' when '39' then
      case when p_taxonomy->>'category_l2' in ('베이커리/빵','떡/전통명과','카페/음료')
        then '카페/디저트' else '음식점' end end
    into expected from public.places p where p.content_id=p_content_id and p.is_active
      and p.modified_time::text is not distinct from p_modified_time for update;
  if not found or expected is null then return false; end if;
  if jsonb_typeof(p_taxonomy) is distinct from 'object'
    or p_taxonomy->>'category_l1' is distinct from expected
    or jsonb_typeof(p_taxonomy->'filter_tags') is distinct from 'array'
    or (p_taxonomy->>'category_l2' is not null and
      (jsonb_typeof(p_taxonomy->'category_l2') is distinct from 'string' or length(p_taxonomy->>'category_l2')>100)) then
    raise exception 'Invalid place taxonomy' using errcode='22023';
  end if;
  if jsonb_array_length(p_taxonomy->'filter_tags')>12 or exists(
    select 1 from jsonb_array_elements(p_taxonomy->'filter_tags') t
    where jsonb_typeof(t) <> 'string' or length(t#>>'{}') not between 1 and 50 or t#>>'{}'='백년가게') then
    raise exception 'Invalid taxonomy tags' using errcode='22023';
  end if;
  -- Manual and public-CSV classification wins over later model classifications.
  update public.places p set
    category_l1=p_taxonomy->>'category_l1',category_l2=p_taxonomy->>'category_l2',
    filter_tags=array(select distinct tag from (
      select jsonb_array_elements_text(p_taxonomy->'filter_tags') as tag
      union all select '백년가게' where p.is_century_store
    ) tags order by tag),taxonomy_source='gemini:v2'
  where p.content_id=p_content_id and (p.taxonomy_source is null or p.taxonomy_source like 'gemini:%');
  get diagnostics affected=row_count;
  return affected>0;
end;
$$;
revoke all on function public.apply_ai_place_taxonomy(text,text,jsonb) from public,anon,authenticated;
grant execute on function public.apply_ai_place_taxonomy(text,text,jsonb) to service_role;
notify pgrst,'reload schema';
commit;
