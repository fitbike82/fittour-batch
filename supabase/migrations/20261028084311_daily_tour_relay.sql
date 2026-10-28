-- TASK-032: filtered relay queue, dynamic population and daily-master recovery.
begin;
alter table public.places add column if not exists enrichment_completed boolean default false;
create table if not exists batch_tour.daily_master_state (
  scope text primary key, last_completed_day date not null
);
alter table batch_tour.daily_master_state enable row level security;
grant select,insert,update on batch_tour.daily_master_state to service_role;

create or replace function public.daily_master_since(p_default date)
returns date language sql stable security invoker set search_path='' as $$
  select least(p_default,coalesce(max(last_completed_day),p_default))
  from batch_tour.daily_master_state where scope='all';
$$;
create or replace function public.complete_daily_master(p_day date)
returns void language plpgsql security invoker set search_path='' as $$
begin
  if p_day is null or p_day>(now() at time zone 'Asia/Seoul')::date then
    raise exception 'Invalid master checkpoint';
  end if;
  insert into batch_tour.daily_master_state(scope,last_completed_day) values('all',p_day)
    on conflict(scope) do update set last_completed_day=greatest(
      batch_tour.daily_master_state.last_completed_day,excluded.last_completed_day);
end;
$$;

create or replace function public.claim_batch_tour_places_filtered(
  p_limit integer default 100,p_after text default '',p_retry_failed boolean default false,p_categories text[] default null
) returns table(content_id text,content_type_id text,title text,claim_token uuid)
language plpgsql security invoker set search_path='' as $$
begin
  if p_limit is null or p_limit not between 1 and 100 or
    (p_categories is not null and (cardinality(p_categories)=0 or
     not p_categories <@ array['12','14','15','25','28','32','38','39']::text[])) then
    raise exception 'Invalid claim selection';
  end if;
  return query
  with candidates as (
    select p.content_id from public.places p
    where p.is_active and p.enrich_status is distinct from 'completed' and p.content_id>p_after
      and p.content_type_id in ('12','14','15','25','28','32','38','39')
      and (p_categories is null or p.content_type_id=any(p_categories))
      and (p.enrich_status is null or (p_retry_failed and p.enrich_status='failed')
        or (p.enrich_status='pending' and (p.enrich_claim_until is null or p.enrich_claim_until<now())))
    order by p.content_id limit p_limit for update skip locked
  ), claimed as (
    update public.places p set enrich_status='pending',enrich_claim_token=gen_random_uuid(),
      enrich_claim_until=now()+interval '30 minutes'
    from candidates c where p.content_id=c.content_id
    returning p.content_id::text,p.content_type_id::text,p.title::text,p.enrich_claim_token
  ) select * from claimed order by claimed.content_id;
end;
$$;

create or replace function public.batch_tour_snapshot(p_key text default null,p_categories text[] default null)
returns jsonb language sql stable security invoker set search_path='' as $$
  with grouped as (
    select p.content_type_id::text as category,count(*) as total,
      count(*) filter(where p.enrich_status='completed') as completed,
      count(*) filter(where p.enrich_status is null) as unprocessed,
      count(*) filter(where p.enrich_status='pending') as pending,
      count(*) filter(where p.enrich_status='failed') as failed,
      count(*) filter(where p.enrichment_completed) as legacy_completed
    from public.places p where p.is_active and p.content_type_id in ('12','14','15','25','28','32','38','39')
    group by p.content_type_id
  ), totals as (
    select coalesce(sum(total),0) as total,coalesce(sum(completed),0) as completed,
      coalesce(sum(unprocessed),0) as unprocessed,coalesce(sum(pending),0) as pending,
      coalesce(sum(failed),0) as failed,coalesce(sum(legacy_completed),0) as legacy_completed
    from grouped where p_categories is null or category=any(p_categories)
  ) select jsonb_build_object(
    'captured_at',now(),'day',(now() at time zone 'Asia/Seoul')::date,
    'database_total',(select count(*) from public.places),
    'active_total',(select coalesce(sum(total),0) from grouped),
    'selected_total',t.total,'completed',t.completed,'remaining',t.total-t.completed,
    'unprocessed',t.unprocessed,'pending',t.pending,'failed',t.failed,
    'legacy_completed',t.legacy_completed,
    'completion_percent',case when t.total=0 then 0 else round(t.completed*100.0/t.total,2) end,
    'categories',(select coalesce(jsonb_agg(to_jsonb(g) order by g.category),'[]'::jsonb) from grouped g),
    'quota',jsonb_build_object('used',coalesce((select calls from batch_tour.daily_calls
      where key_id=p_key and day=(now() at time zone 'Asia/Seoul')::date),0),'limit',100000,
      'blocked',coalesce((select blocked from batch_tour.daily_calls
      where key_id=p_key and day=(now() at time zone 'Asia/Seoul')::date),false))
  ) from totals t;
$$;

create or replace function public.invalidate_batch_tour_place()
returns trigger language plpgsql security invoker set search_path='' as $$
begin
  if new.modified_time is distinct from old.modified_time
     or new.content_type_id is distinct from old.content_type_id then
    new.enrichment_completed := false;
    new.enrich_status := null;
    new.enrich_claim_token := null;
    new.enrich_claim_until := null;
  end if;
  return new;
end;
$$;
create or replace function public.complete_batch_tour_place(
  p_content_id text,p_token uuid,p_overview text,p_intro jsonb,p_course jsonb,p_stops jsonb
) returns boolean language plpgsql security invoker set search_path='' as $$
declare kind text;
begin
  select p.content_type_id::text into kind from public.places p
  where p.content_id=p_content_id and p.enrich_claim_token=p_token and p.enrich_status='pending'
    and p.enrich_claim_until>now() for update;
  if not found then return false; end if;
  if jsonb_typeof(p_intro) is distinct from 'object' then raise exception 'Invalid intro'; end if;
  if kind='25' then
    if jsonb_typeof(p_course) is distinct from 'object' or jsonb_typeof(p_stops) is distinct from 'array'
       or jsonb_array_length(p_stops)=0 or nullif(p_course->>'title','') is null then
      raise exception 'Incomplete course';
    end if;
    insert into public.tour_courses(contentid,title,distance,taketime,schedule)
      values(p_content_id,p_course->>'title',p_course->>'distance',p_course->>'taketime',p_course->>'schedule')
    on conflict(contentid) do update set title=excluded.title,distance=excluded.distance,
      taketime=excluded.taketime,schedule=excluded.schedule;
    delete from public.tour_course_places where course_contentid=p_content_id;
    insert into public.tour_course_places(course_contentid,subnum,subcontentid,subname,subdetailoverview,subdetailimg)
      select p_content_id,r.subnum,r.subcontentid,r.subname,r.subdetailoverview,r.subdetailimg
      from jsonb_to_recordset(p_stops) as r(subnum integer,subcontentid text,subname text,subdetailoverview text,subdetailimg text);
  end if;
  update public.places set overview=p_overview,detail_intro=p_intro,enrich_status='completed',enrichment_completed=true,
    enrich_claim_token=null,enrich_claim_until=null where public.places.content_id=p_content_id;
  return true;
end;
$$;


create or replace function public.batch_tour_relay_preflight()
returns boolean language sql stable security invoker set search_path='' as $$
  select public.batch_tour_preflight()
    and to_regclass('batch_tour.daily_master_state') is not null
    and to_regprocedure('public.claim_batch_tour_places_filtered(integer,text,boolean,text[])') is not null;
$$;
revoke all on function public.daily_master_since(date),public.complete_daily_master(date),
  public.claim_batch_tour_places_filtered(integer,text,boolean,text[]),
  public.batch_tour_snapshot(text,text[]),public.batch_tour_relay_preflight()
  from public,anon,authenticated;
grant execute on function public.daily_master_since(date),public.complete_daily_master(date),
  public.claim_batch_tour_places_filtered(integer,text,boolean,text[]),
  public.batch_tour_snapshot(text,text[]),public.batch_tour_relay_preflight() to service_role;
commit;
