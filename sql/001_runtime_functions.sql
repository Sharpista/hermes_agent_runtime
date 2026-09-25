-- Apply only after the agent_runs, agent_execution_locks and agent_events tables exist.
-- These RPCs are deliberately available to service_role only. No SECURITY DEFINER.
begin;

-- Allows the script to be re-applied safely when function signatures or return types change.
drop function if exists public.hermes_claim_run(text,text,text,text,text,text,integer);
drop function if exists public.hermes_heartbeat_run(text,text,integer);
drop function if exists public.hermes_finish_run(text,text,text,jsonb);
drop function if exists public.hermes_recover_expired_lock(text);

create or replace function public.hermes_claim_run(
  p_run_id text, p_issue_id text, p_agent text, p_risk text,
  p_mode text, p_environment text, p_ttl_seconds integer
) returns boolean language plpgsql security invoker set search_path = '' as $$
declare
  v_expires_at timestamptz;
  v_holder_run text;
  v_holder_agent text;
begin
  if p_ttl_seconds < 10 or p_ttl_seconds > 86400 then
    raise exception 'Invalid lock TTL';
  end if;
  -- Serializes claims against recovery for this issue. The unique index/PK is the final guard.
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_issue_id, 0));
  v_expires_at := pg_catalog.now() + pg_catalog.make_interval(secs => p_ttl_seconds);
  begin
    -- Keep the run and lock inserts in the same exception scope so either both succeed or
    -- the conflict path returns false without leaving a partial rejected run behind.
    insert into public.agent_runs
      (run_id, linear_issue_id, agent, status, risk, execution_mode, environment)
    values (p_run_id, p_issue_id, p_agent, 'queued', p_risk, p_mode, p_environment);
    insert into public.agent_execution_locks
      (linear_issue_id, run_id, agent, expires_at)
    values (p_issue_id, p_run_id, p_agent, v_expires_at);
  exception when unique_violation then
    -- The failed subtransaction rolls back the attempted insert(s). Audit the holder instead
    -- of creating/updating a synthetic failed run for the rejected attempt.
    select l.run_id, l.agent into v_holder_run, v_holder_agent
    from public.agent_execution_locks l
    where l.linear_issue_id = p_issue_id;
    if v_holder_run is null then
      select r.run_id, r.agent into v_holder_run, v_holder_agent
      from public.agent_runs r
      where r.linear_issue_id = p_issue_id
        and r.status in ('queued', 'running', 'reviewing', 'blocked')
      order by r.started_at desc
      limit 1;
    end if;
    if v_holder_run is not null then
      insert into public.agent_events (run_id, linear_issue_id, agent, event_type, payload)
      values (v_holder_run, p_issue_id, v_holder_agent, 'lock.rejected',
              pg_catalog.jsonb_build_object('reason', 'RunConflict',
                                            'attempted_run_id', p_run_id));
    end if;
    return false;
  end;
  update public.agent_runs set status = 'running', heartbeat_at = pg_catalog.now()
  where run_id = p_run_id and linear_issue_id = p_issue_id;
  insert into public.agent_events (run_id, linear_issue_id, agent, event_type, payload)
  values (p_run_id, p_issue_id, p_agent, 'run.created', '{}'::jsonb),
         (p_run_id, p_issue_id, p_agent, 'lock.acquired',
          pg_catalog.jsonb_build_object('ttl_seconds', p_ttl_seconds, 'expires_at', v_expires_at)),
         (p_run_id, p_issue_id, p_agent, 'run.started',
          pg_catalog.jsonb_build_object('risk', p_risk, 'execution_mode', p_mode, 'environment', p_environment));
  return true;
end;
$$;

create or replace function public.hermes_heartbeat_run(
  p_run_id text, p_issue_id text, p_ttl_seconds integer
) returns boolean language plpgsql security invoker set search_path = '' as $$
declare v_agent text;
begin
  if p_ttl_seconds < 10 or p_ttl_seconds > 86400 then
    raise exception 'Invalid lock TTL';
  end if;
  update public.agent_execution_locks
  set heartbeat_at = pg_catalog.now(),
      expires_at = pg_catalog.now() + pg_catalog.make_interval(secs => p_ttl_seconds)
  where linear_issue_id = p_issue_id and run_id = p_run_id
    and expires_at > pg_catalog.now()
  returning agent into v_agent;
  if v_agent is null then return false; end if;
  update public.agent_runs set heartbeat_at = pg_catalog.now()
  where run_id = p_run_id and status in ('running', 'reviewing');
  return found;
end;
$$;

create or replace function public.hermes_finish_run(
  p_run_id text, p_issue_id text, p_status text, p_fields jsonb default '{}'::jsonb
) returns boolean language plpgsql security invoker set search_path = '' as $$
declare
  v_agent text;
  v_fields jsonb;
  v_payload jsonb;
begin
  if p_status not in ('completed', 'failed', 'blocked', 'canceled') then
    raise exception 'Invalid final status';
  end if;
  -- A non-object p_fields would make every ->> lookup unpredictable; fail before any write.
  v_fields := coalesce(p_fields, '{}'::jsonb);
  if pg_catalog.jsonb_typeof(v_fields) <> 'object' then
    raise exception 'p_fields must be a JSON object';
  end if;
  select agent into v_agent from public.agent_execution_locks
  where linear_issue_id = p_issue_id and run_id = p_run_id for update;
  if not found then return false; end if;
  update public.agent_runs set
    status = p_status,
    finished_at = pg_catalog.now(),
    commit_sha = v_fields->>'commit_sha',
    pull_request_url = v_fields->>'pull_request_url',
    railway_deployment_id = v_fields->>'railway_deployment_id',
    tests_status = v_fields->>'tests_status',
    review_status = v_fields->>'review_status',
    error = v_fields->>'error'
  where run_id = p_run_id and linear_issue_id = p_issue_id
    and status in ('running', 'reviewing');
  if not found then return false; end if;
  -- Payload da allowlist da Spec 009 secao 6.4 por status final; jsonb_strip_nulls evita
  -- chaves nulas e o fallback de erro mantem um codigo curto sanitizado.
  v_payload := case p_status
    when 'completed' then pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
      'commit_sha', v_fields->>'commit_sha',
      'pull_request_url', v_fields->>'pull_request_url',
      'railway_deployment_id', v_fields->>'railway_deployment_id',
      'tests_status', v_fields->>'tests_status',
      'review_status', v_fields->>'review_status'))
    when 'failed' then pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
      'error', coalesce(nullif(v_fields->>'error', ''), 'RuntimeError'),
      'error_code', v_fields->>'error_code',
      'kanban_task_id', v_fields->>'kanban_task_id',
      'kanban_outcome', v_fields->>'kanban_outcome'))
    when 'blocked' then pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
      'error', coalesce(nullif(v_fields->>'error', ''), 'ExecutionBlocked'),
      'blocked_reason', v_fields->>'blocked_reason',
      'kanban_task_id', v_fields->>'kanban_task_id',
      'tests_status', v_fields->>'tests_status',
      'review_status', v_fields->>'review_status'))
    when 'canceled' then pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
      'canceled_by', v_fields->>'canceled_by',
      'reason', v_fields->>'reason'))
    else '{}'::jsonb
  end;
  insert into public.agent_events (run_id, linear_issue_id, agent, event_type, payload)
  values (p_run_id, p_issue_id, v_agent, 'run.' || p_status, v_payload),
         (p_run_id, p_issue_id, v_agent, 'lock.released',
          pg_catalog.jsonb_build_object('finished_status', p_status));
  delete from public.agent_execution_locks
  where linear_issue_id = p_issue_id and run_id = p_run_id;
  return true;
end;
$$;

-- 'blocked' is terminal in the runtime but remains in the partial unique index
-- uq_agent_runs_active_issue: no new claim can enter the issue until an operator
-- releases it. Runbook (service_role / dashboard, never by the runtime):
--   update public.agent_runs set status = 'canceled', finished_at = now(),
--          error = 'ReleasedByOperator'
--   where linear_issue_id = '<ISSUE>' and status = 'blocked';
create or replace function public.hermes_recover_expired_lock(p_issue_id text)
returns boolean language plpgsql security invoker set search_path = '' as $$
declare v_lock public.agent_execution_locks%rowtype;
begin
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_issue_id, 0));
  select * into v_lock from public.agent_execution_locks
  where linear_issue_id = p_issue_id for update;
  if not found or v_lock.expires_at > pg_catalog.now() then return false; end if;
  update public.agent_runs set status = 'failed', finished_at = pg_catalog.now(),
    error = 'LockExpired'
  where run_id = v_lock.run_id and status in ('running', 'reviewing', 'queued');
  insert into public.agent_events (run_id, linear_issue_id, agent, event_type, payload)
  values (v_lock.run_id, p_issue_id, v_lock.agent, 'lock.expired',
          pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
            'expired_at', v_lock.expires_at,
            'last_heartbeat_at', v_lock.heartbeat_at))),
         (v_lock.run_id, p_issue_id, v_lock.agent, 'lock.recovered',
          pg_catalog.jsonb_build_object('previous_run_id', v_lock.run_id));
  delete from public.agent_execution_locks
  where linear_issue_id = p_issue_id and run_id = v_lock.run_id;
  return true;
end;
$$;

revoke all on function public.hermes_claim_run(text,text,text,text,text,text,integer) from public, anon, authenticated;
revoke all on function public.hermes_heartbeat_run(text,text,integer) from public, anon, authenticated;
revoke all on function public.hermes_finish_run(text,text,text,jsonb) from public, anon, authenticated;
revoke all on function public.hermes_recover_expired_lock(text) from public, anon, authenticated;
grant execute on function public.hermes_claim_run(text,text,text,text,text,text,integer) to service_role;
grant execute on function public.hermes_heartbeat_run(text,text,integer) to service_role;
grant execute on function public.hermes_finish_run(text,text,text,jsonb) to service_role;
grant execute on function public.hermes_recover_expired_lock(text) to service_role;

-- Harden the sequence beyond the default Supabase grants for anon/authenticated.
revoke all on sequence public.agent_events_id_seq from anon, authenticated;
grant usage, select on sequence public.agent_events_id_seq to service_role;

commit;
