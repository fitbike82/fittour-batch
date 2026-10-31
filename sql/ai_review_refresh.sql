-- Apply after ai_review_two_pass_queue.sql, before deploying the new worker.
begin;
alter table public.place_ai_summaries add column if not exists last_checked_at timestamptz;
create table if not exists public.ai_review_summary_history (
  id bigint generated always as identity primary key,
  content_id varchar not null references public.places(content_id) on delete cascade,
  snapshot jsonb not null,
  archived_at timestamptz not null default now()
);
alter table public.ai_review_summary_history enable row level security;
revoke all on public.ai_review_summary_history from public,anon,authenticated;
grant select,insert on public.ai_review_summary_history to service_role;
grant usage,select on sequence public.ai_review_summary_history_id_seq to service_role;
create index if not exists ai_review_history_place_idx on public.ai_review_summary_history(content_id,archived_at desc);

create or replace function public.archive_ai_review_summary()
returns trigger language plpgsql security invoker set search_path='' as $$
begin
  if old.quality_status='ready' and row(old.honest_summary,old.pros,old.cons,old.source_reviews)
    is distinct from row(new.honest_summary,new.pros,new.cons,new.source_reviews) then
    insert into public.ai_review_summary_history(content_id,snapshot) values(old.content_id,to_jsonb(old));
  end if;
  return new;
end;
$$;
drop trigger if exists archive_ai_review_summary on public.place_ai_summaries;
create trigger archive_ai_review_summary before update on public.place_ai_summaries
for each row execute function public.archive_ai_review_summary();
revoke all on function public.archive_ai_review_summary() from public,anon,authenticated;
grant execute on function public.archive_ai_review_summary() to service_role;

create or replace function public.enqueue_due_ai_reviews(batch_limit integer default 50,refresh_days integer default 180)
returns integer language plpgsql security invoker set search_path='' as $$
declare affected integer;
begin
  if batch_limit is null or batch_limit not between 1 and 120 or refresh_days is null or refresh_days not between 1 and 3650 then
    raise exception 'Invalid refresh configuration' using errcode='22023';
  end if;
  perform pg_advisory_xact_lock(62020120);
  -- Bootstrap missing jobs, including newly active places. Coverage takes priority.
  insert into public.ai_review_jobs(content_id,input_modified_time)
  select p.content_id,p.modified_time::text from public.places p
  left join public.place_ai_summaries s using(content_id)
  where p.is_active and (s.content_id is null or p.needs_ai_summary)
  on conflict(content_id) do nothing;
  if exists(select 1 from public.ai_review_jobs j join public.places p using(content_id)
    where p.is_active and j.last_status<>'ready' and j.attempt_count<2
      and p.modified_time::text is not distinct from j.input_modified_time) then return 0; end if;
  with due as (
    select p.content_id,p.modified_time::text as revision
    from public.places p left join public.ai_review_jobs j using(content_id)
    left join public.place_ai_summaries s using(content_id)
    where p.is_active and (j.leased_until is null or j.leased_until<=now())
      and greatest(j.attempted_at,s.updated_at) <= now()-make_interval(days=>refresh_days)
    order by greatest(j.attempted_at,s.updated_at),p.content_id limit batch_limit
  )
  insert into public.ai_review_jobs(content_id,input_modified_time)
  select content_id,revision from due
  on conflict(content_id) do update set input_modified_time=excluded.input_modified_time,
    attempt_count=0,last_status='pending',last_error_code=null,attempted_at=null,
    lease_token=null,leased_until=null,updated_at=now();
  get diagnostics affected=row_count;
  return affected;
end;
$$;
revoke all on function public.enqueue_due_ai_reviews(integer,integer) from public,anon,authenticated;
grant execute on function public.enqueue_due_ai_reviews(integer,integer) to service_role;

-- Completion/trigger replacements appended below; atomic with the rollout.

create or replace function public.enqueue_ai_review_revision()
returns trigger language plpgsql security invoker set search_path='' as $$
begin
  if new.is_active then
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
    or jsonb_array_length(summary_data->'pros') not between 0 and 3
    or jsonb_array_length(summary_data->'cons') not between 0 and 5) then
    raise exception 'Invalid ready summary' using errcode='22023';
  end if;
  if result_status<>'error' then
    insert into public.place_ai_summaries(content_id,quality_status,honest_summary,pros,cons,visit_tips,model,source_reviews,updated_at)
    values(job_content_id,result_status,
      case when result_status='ready' then (select string_agg(value,E'\n') from jsonb_array_elements_text(summary_data->'summary_lines')) else null end,
      coalesce(summary_data->'pros','[]'),coalesce(summary_data->'cons','[]'),summary_data->>'visit_tips',model_name,
      jsonb_build_object('input_modified_time',input_modified_time,'reason',reason_code,'reviews',reviews_data,
        'attempt_number',attempt,'diagnostics',diagnostics_data,'analyzed_at',now(),
        'analysis_basis',coalesce(summary_data->>'analysis_basis','review_analysis')),now())
    on conflict(content_id) do update set quality_status=excluded.quality_status,honest_summary=excluded.honest_summary,
      pros=excluded.pros,cons=excluded.cons,visit_tips=excluded.visit_tips,model=excluded.model,
      source_reviews=excluded.source_reviews,updated_at=excluded.updated_at
    where public.place_ai_summaries.quality_status<>'ready'
       or (excluded.quality_status='ready' and (coalesce(excluded.source_reviews->>'analysis_basis','review_analysis')<>'basic_info'
           or coalesce(public.place_ai_summaries.source_reviews->>'analysis_basis','review_analysis')='basic_info'));
  end if;
  update public.place_ai_summaries set last_checked_at=now() where content_id=job_content_id;
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


notify pgrst,'reload schema';
commit;
