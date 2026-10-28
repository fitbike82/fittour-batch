-- TASK-030/031. The deployed places PK is content_id, not TourAPI's contentid.
begin;
alter table public.places
  add column if not exists enrich_status text default null,
  add column if not exists detail_intro jsonb,
  add column if not exists overview text,
  add column if not exists enrich_claim_token uuid,
  add column if not exists enrich_claim_until timestamptz;
do $$ begin
  if not exists (select 1 from pg_constraint where conrelid='public.places'::regclass and conname='places_enrich_status_check') then
    alter table public.places add constraint places_enrich_status_check
      check (enrich_status is null or enrich_status in ('pending','completed','failed'));
  end if;
end $$;
create index if not exists places_fast_enrichment_queue_idx on public.places(content_id)
  where is_active and enrich_status is distinct from 'completed';

create table if not exists public.tour_courses (
  contentid text primary key references public.places(content_id) on delete cascade,
  title text not null, distance text, taketime text, schedule text,
  created_at timestamptz not null default now()
);
create table if not exists public.tour_course_places (
  id uuid primary key default gen_random_uuid(),
  course_contentid text not null references public.tour_courses(contentid) on delete cascade,
  subnum integer not null check (subnum > 0), subcontentid text,
  subname text not null, subdetailoverview text, subdetailimg text,
  unique(course_contentid, subnum)
);
alter table public.tour_courses enable row level security;
alter table public.tour_course_places enable row level security;
revoke all on public.tour_courses, public.tour_course_places from anon, authenticated;
grant select on public.tour_courses, public.tour_course_places to anon, authenticated;
grant select,insert,update,delete on public.tour_courses, public.tour_course_places to service_role;
drop policy if exists active_course_read on public.tour_courses;
create policy active_course_read on public.tour_courses for select to anon, authenticated
  using (exists(select 1 from public.places p where p.content_id=contentid and p.is_active));
drop policy if exists active_course_stop_read on public.tour_course_places;
create policy active_course_stop_read on public.tour_course_places for select to anon, authenticated
  using (exists(select 1 from public.places p where p.content_id=course_contentid and p.is_active));

create schema if not exists batch_tour;
revoke all on schema batch_tour from public, anon, authenticated;
grant usage on schema batch_tour to service_role;
create table if not exists batch_tour.daily_calls (
  key_id text not null, day date not null,
  calls integer not null default 0 check(calls between 0 and 100000),
  blocked boolean not null default false,
  primary key(key_id, day)
);
alter table batch_tour.daily_calls enable row level security;
grant select,insert,update on batch_tour.daily_calls to service_role;

create or replace function public.reserve_batch_tour_call(
  p_key text, p_cap integer default 100000, p_floor integer default 0, p_block boolean default false
) returns boolean language plpgsql security invoker set search_path='' as $$
declare today date := (clock_timestamp() at time zone 'Asia/Seoul')::date; allowed boolean;
begin
  if p_key !~ '^[0-9a-f]{64}$' or p_cap not between 1 and 100000 or p_floor not between 0 and 100000 then
    raise exception 'Invalid quota configuration';
  end if;
  insert into batch_tour.daily_calls(key_id,day,calls,blocked) values(p_key,today,p_floor,p_block)
    on conflict(key_id,day) do update set
      calls=greatest(batch_tour.daily_calls.calls,p_floor),
      blocked=batch_tour.daily_calls.blocked or p_block;
  update batch_tour.daily_calls set calls=calls+1
    where key_id=p_key and day=today and not blocked and calls<p_cap
    returning true into allowed;
  return coalesce(allowed,false);
end;
$$;

create or replace function public.invalidate_batch_tour_place()
returns trigger language plpgsql security invoker set search_path='' as $$
begin
  if new.modified_time is distinct from old.modified_time
     or new.content_type_id is distinct from old.content_type_id then
    new.enrich_status := null;
    new.enrich_claim_token := null;
    new.enrich_claim_until := null;
  end if;
  return new;
end;
$$;
drop trigger if exists invalidate_batch_tour_place on public.places;
create trigger invalidate_batch_tour_place before update of modified_time,content_type_id on public.places
  for each row execute function public.invalidate_batch_tour_place();

create or replace function public.claim_batch_tour_places(
  p_limit integer default 100, p_after text default '', p_retry_failed boolean default false
) returns table(content_id text,content_type_id text,title text,claim_token uuid)
language plpgsql security invoker set search_path='' as $$
begin
  if p_limit is null or p_limit not between 1 and 100 then raise exception 'Invalid claim size'; end if;
  return query
  with candidates as (
    select p.content_id from public.places p
    where p.is_active and p.enrich_status is distinct from 'completed' and p.content_id>p_after
      and p.content_type_id in ('12','14','15','25','28','32','38','39')
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
  update public.places set overview=p_overview,detail_intro=p_intro,enrich_status='completed',
    enrich_claim_token=null,enrich_claim_until=null where public.places.content_id=p_content_id;
  return true;
end;
$$;

create or replace function public.release_batch_tour_place(p_content_id text,p_token uuid,p_failed boolean default false)
returns void language sql security invoker set search_path='' as $$
  update public.places set enrich_status=case when p_failed then 'failed' else null end,
    enrich_claim_token=null,enrich_claim_until=null
  where content_id=p_content_id and enrich_claim_token=p_token and enrich_status='pending';
$$;
create or replace function public.batch_tour_preflight()
returns boolean language sql stable security invoker set search_path='' as $$
  select to_regclass('public.tour_courses') is not null
     and to_regclass('public.tour_course_places') is not null
     and to_regclass('batch_tour.daily_calls') is not null
     and exists(select 1 from information_schema.columns
       where table_schema='public' and table_name='places' and column_name='detail_intro');
$$;
revoke all on function public.reserve_batch_tour_call(text,integer,integer,boolean),
  public.invalidate_batch_tour_place(), public.claim_batch_tour_places(integer,text,boolean),
  public.complete_batch_tour_place(text,uuid,text,jsonb,jsonb,jsonb),
  public.release_batch_tour_place(text,uuid,boolean),public.batch_tour_preflight()
  from public,anon,authenticated;
grant execute on function public.reserve_batch_tour_call(text,integer,integer,boolean),
  public.invalidate_batch_tour_place(),public.claim_batch_tour_places(integer,text,boolean),
  public.complete_batch_tour_place(text,uuid,text,jsonb,jsonb,jsonb),
  public.release_batch_tour_place(text,uuid,boolean),public.batch_tour_preflight() to service_role;
commit;
