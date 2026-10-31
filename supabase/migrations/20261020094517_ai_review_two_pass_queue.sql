begin;

-- Private operational state; public browsing permissions are unchanged.
create table if not exists public.ai_review_jobs (
  content_id varchar primary key references public.places(content_id) on delete cascade,
  input_modified_time text,
  attempt_count smallint not null default 0 check (attempt_count between 0 and 2),
  last_status text not null default 'pending' check (last_status in ('pending','ready','insufficient','error')),
  last_error_code text,
  attempted_at timestamptz,
  lease_token uuid,
  leased_until timestamptz,
  updated_at timestamptz not null default now()
);
create table if not exists public.ai_review_attempts (
  id bigint generated always as identity primary key,
  content_id varchar not null references public.places(content_id) on delete cascade,
  input_modified_time text,
  attempt_number smallint not null check (attempt_number between 1 and 2),
  status text not null check (status in ('ready','insufficient','error')),
  reason_code text,
  model text,
  diagnostics jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index if not exists ai_review_jobs_pending_idx
  on public.ai_review_jobs(attempt_count,content_id) where attempt_count < 2 and last_status <> 'ready';
create index if not exists ai_review_attempts_place_idx on public.ai_review_attempts(content_id,created_at desc);
alter table public.ai_review_jobs enable row level security;
alter table public.ai_review_attempts enable row level security;
revoke all on public.ai_review_jobs,public.ai_review_attempts from public,anon,authenticated;
grant select,insert,update,delete on public.ai_review_jobs,public.ai_review_attempts to service_role;
grant usage,select on sequence public.ai_review_attempts_id_seq to service_role;

-- Existing insufficient records have already used their first attempt. Reapplying
-- this migration must never reset a completed second attempt or an active lease.
insert into public.ai_review_jobs(content_id,input_modified_time,attempt_count,last_status,last_error_code,attempted_at)
select p.content_id,p.modified_time::text,
  case when s.quality_status='insufficient' and (s.source_reviews->>'input_modified_time') is not distinct from p.modified_time::text then 1 else 0 end,
  case when s.quality_status='insufficient' and (s.source_reviews->>'input_modified_time') is not distinct from p.modified_time::text then 'insufficient' else 'pending' end,
  s.source_reviews->>'reason',s.updated_at
from public.places p left join public.place_ai_summaries s using(content_id)
where p.is_active and (p.needs_ai_summary or s.quality_status='insufficient')
  and not (coalesce(s.quality_status='ready',false)
    and (s.source_reviews->>'input_modified_time') is not distinct from p.modified_time::text)
on conflict(content_id) do nothing;

insert into public.ai_review_attempts(content_id,input_modified_time,attempt_number,status,reason_code,model,diagnostics,created_at)
select j.content_id,j.input_modified_time,1,'insufficient',j.last_error_code,s.model,
  jsonb_build_object('legacy_backfill',true),coalesce(j.attempted_at,now())
from public.ai_review_jobs j join public.place_ai_summaries s using(content_id)
where j.attempt_count=1 and j.last_status='insufficient'
  and not exists(select 1 from public.ai_review_attempts a where a.content_id=j.content_id
    and a.input_modified_time is not distinct from j.input_modified_time and a.attempt_number=1);

create or replace function public.enqueue_ai_review_revision()
returns trigger language plpgsql security invoker set search_path='' as $$
begin
  if new.is_active and new.needs_ai_summary then
    if tg_op='INSERT' or new.modified_time is distinct from old.modified_time
      or new.title is distinct from old.title or new.address is distinct from old.address
      or new.content_type_id is distinct from old.content_type_id
      or (not coalesce(old.needs_ai_summary,false) and new.needs_ai_summary) then
      insert into public.ai_review_jobs(content_id,input_modified_time)
      values(new.content_id,new.modified_time::text)
      on conflict(content_id) do update set input_modified_time=excluded.input_modified_time,
        attempt_count=0,last_status='pending',last_error_code=null,attempted_at=null,
        lease_token=null,leased_until=null,updated_at=now();
    end if;
  end if;
  return new;
end;
$$;
drop trigger if exists enqueue_ai_review_revision on public.places;
create trigger enqueue_ai_review_revision after insert or update of modified_time,title,address,content_type_id,needs_ai_summary,is_active
on public.places for each row execute function public.enqueue_ai_review_revision();

create or replace function public.claim_ai_review_work(batch_limit integer default 120)
returns table(content_id text,content_type_id text,title text,address text,modified_time text,
  attempt_number smallint,claim_token uuid)
language plpgsql security invoker set search_path='' as $$
declare
  phase smallint;
  token uuid := gen_random_uuid();
begin
  if batch_limit is null or batch_limit not between 1 and 120 then
    raise exception 'batch_limit must be between 1 and 120' using errcode='22023';
  end if;
  perform pg_advisory_xact_lock(62020120);
  -- A legacy runner may finish during rollout. Reconcile only results written
  -- after this unclaimed job was created; never replay an older rejection or
  -- alter a job held by a new worker.
  with reconciled as (
    update public.ai_review_jobs j set attempt_count=1,last_status=s.quality_status,
      last_error_code=s.source_reviews->>'reason',attempted_at=s.updated_at,updated_at=now()
    from public.place_ai_summaries s
    where s.content_id=j.content_id and j.attempt_count=0 and j.lease_token is null
      and s.updated_at>j.updated_at and s.quality_status in ('ready','insufficient')
      and (s.source_reviews->>'input_modified_time') is not distinct from j.input_modified_time
    returning j.*
  )
  insert into public.ai_review_attempts(content_id,input_modified_time,attempt_number,status,reason_code,diagnostics,created_at)
  select r.content_id,r.input_modified_time,1,r.last_status,r.last_error_code,
    jsonb_build_object('legacy_reconciled',true),r.attempted_at from reconciled r;
  -- Include leased first-pass jobs in the barrier. Another worker must not begin
  -- retries while any active first-pass work is unfinished.
  if exists(select 1 from public.ai_review_jobs j join public.places p using(content_id)
    where p.is_active and j.attempt_count=0 and j.last_status<>'ready'
      and p.modified_time::text is not distinct from j.input_modified_time) then phase:=0;
  else phase:=1; end if;
  return query
  with candidates as (
    select j.content_id from public.ai_review_jobs j join public.places p using(content_id)
    where p.is_active and j.last_status<>'ready' and j.attempt_count=phase
      and p.modified_time::text is not distinct from j.input_modified_time
      and (j.leased_until is null or j.leased_until<=now())
    order by case when p.content_type_id in ('12','14','15','25','28') then 0
      when p.content_type_id='39' then 1 when p.content_type_id='32' then 2 else 3 end,
      j.content_id
    limit batch_limit for update of j skip locked
  ), claimed as (
    update public.ai_review_jobs j set lease_token=token,leased_until=now()+interval '55 minutes',updated_at=now()
    from candidates c where j.content_id=c.content_id returning j.*
  )
  select p.content_id::text,p.content_type_id::text,p.title::text,p.address::text,p.modified_time::text,
    (j.attempt_count+1)::smallint,token
  from claimed j join public.places p using(content_id)
  order by case when p.content_type_id in ('12','14','15','25','28') then 0
    when p.content_type_id='39' then 1 when p.content_type_id='32' then 2 else 3 end,p.content_id;
end;
$$;

create or replace function public.complete_ai_review_work(
  job_content_id text,claim_token uuid,input_modified_time text,result_status text,
  summary_data jsonb default null,reviews_data jsonb default '[]',model_name text default null,
  reason_code text default null,diagnostics_data jsonb default '{}'
) returns boolean language plpgsql security invoker set search_path='' as $$
declare
  job public.ai_review_jobs%rowtype;
  attempt smallint;
begin
  if result_status is null or result_status not in ('ready','insufficient','error') then
    raise exception 'Invalid review result' using errcode='22023';
  end if;
  -- Lock both records before checking revision and saving: stale workers cannot
  -- overwrite the result for a newly synchronized place revision.
  perform 1 from public.places p where p.content_id=job_content_id and p.is_active
    and p.modified_time::text is not distinct from complete_ai_review_work.input_modified_time for update;
  if not found then return false; end if;
  select j.* into job from public.ai_review_jobs j where j.content_id=job_content_id
    and j.lease_token=claim_token and j.leased_until>now()
    and j.input_modified_time is not distinct from complete_ai_review_work.input_modified_time for update;
  if not found or job.attempt_count>=2 or job.last_status='ready' then return false; end if;
  attempt:=job.attempt_count+1;
  if result_status='ready' and (summary_data is null
    or jsonb_typeof(summary_data->'summary_lines') is distinct from 'array'
    or jsonb_array_length(summary_data->'summary_lines')<>3
    or jsonb_typeof(summary_data->'pros') is distinct from 'array'
    or jsonb_typeof(summary_data->'cons') is distinct from 'array'
    or jsonb_array_length(summary_data->'pros') not between 2 and 3
    or jsonb_array_length(summary_data->'cons')<1) then
    raise exception 'Invalid ready summary' using errcode='22023';
  end if;
  if result_status<>'error' then
    insert into public.place_ai_summaries(content_id,quality_status,honest_summary,pros,cons,visit_tips,model,source_reviews,updated_at)
    values(job_content_id,result_status,
      case when result_status='ready' then (select string_agg(value,E'\n') from jsonb_array_elements_text(summary_data->'summary_lines')) else null end,
      coalesce(summary_data->'pros','[]'),coalesce(summary_data->'cons','[]'),summary_data->>'visit_tips',model_name,
      jsonb_build_object('input_modified_time',input_modified_time,'reason',reason_code,'reviews',reviews_data,
        'attempt_number',attempt,'diagnostics',diagnostics_data),now())
    on conflict(content_id) do update set quality_status=excluded.quality_status,honest_summary=excluded.honest_summary,
      pros=excluded.pros,cons=excluded.cons,visit_tips=excluded.visit_tips,model=excluded.model,
      source_reviews=excluded.source_reviews,updated_at=excluded.updated_at;
  end if;
  insert into public.ai_review_attempts(content_id,input_modified_time,attempt_number,status,reason_code,model,diagnostics)
  values(job_content_id,input_modified_time,attempt,result_status,reason_code,model_name,diagnostics_data);
  -- Update the legacy flag first. The enqueue trigger sees true->true on a first
  -- exclusion/error and therefore cannot reset the attempt counter.
  update public.places set needs_ai_summary=(result_status<>'ready' and attempt<2) where content_id=job_content_id;
  update public.ai_review_jobs set attempt_count=attempt,last_status=result_status,last_error_code=reason_code,
    attempted_at=now(),lease_token=null,leased_until=null,updated_at=now() where content_id=job_content_id;
  return true;
end;
$$;

create or replace function public.release_ai_review_work(claim_token uuid)
returns void language sql security invoker set search_path='' as $$
  update public.ai_review_jobs set lease_token=null,leased_until=null,updated_at=now() where lease_token=claim_token;
$$;

revoke all on function public.enqueue_ai_review_revision() from public,anon,authenticated;
revoke all on function public.claim_ai_review_work(integer) from public,anon,authenticated;
revoke all on function public.complete_ai_review_work(text,uuid,text,text,jsonb,jsonb,text,text,jsonb) from public,anon,authenticated;
revoke all on function public.release_ai_review_work(uuid) from public,anon,authenticated;
grant execute on function public.enqueue_ai_review_revision(),public.claim_ai_review_work(integer),
  public.complete_ai_review_work(text,uuid,text,text,jsonb,jsonb,text,text,jsonb),public.release_ai_review_work(uuid) to service_role;
notify pgrst,'reload schema';
commit;
