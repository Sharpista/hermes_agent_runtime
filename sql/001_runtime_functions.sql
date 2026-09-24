-- Apply only after the agent_runs, agent_execution_locks and agent_events tables exist.
-- These RPCs are deliberately available to service_role only. No SECURITY DEFINER.
begin;

create or replace function public.hermes_claim_run(
  p_run_id text, p_issue_id text, p_agent text, p_risk text,
  p_mode text, p_environment text, p_ttl_seconds integer
) returns boolean language plpgsql security invoker set search_path = '' as $$
begin
  if p_ttl_seconds < 10 or p_ttl_seconds > 86400 then
    raise exception 'Invalid lock TTL';
  end if;
  -- Serializes claims against recovery for this issue. The unique index/PK is the final guard.
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_issue_id, 0));
  insert into public.agent_runs
    (run_id, linear_issue_id, agent, status, risk, execution_mode, environment)
  values (p_run_id, p_issue_id, p_agent, 'queued', p_risk, p_mode, p_environment);
  begin
    insert into public.agent_execution_locks
      (linear_issue_id, run_id, agent, expires_at)
    values (p_issue_id, p_run_id, p_agent, pg_catalog.now() + pg_catalog.make_interval(secs => p_ttl_seconds));
  exception when unique_violation then
    update public.agent_runs set status = 'failed', finished_at = pg_catalog.now(), error = 'RunConflict'
    where run_id = p_run_id and linear_issue_id = p_issue_id;
    insert into public.agent_events (run_id, linear_issue_id, agent, event_type, payload)
    values (p_run_id, p_issue_id, p_agent, 'lock.rejected',
            pg_catalog.jsonb_build_object('reason', 'RunConflict')),
           (p_run_id, p_issue_id, p_agent, 'run.failed',
            pg_catalog.jsonb_build_object('error', 'RunConflict'));
    return false;
  end;
  update public.agent_runs set status = 'running', heartbeat_at = pg_catalog.now()
  where run_id = p_run_id and linear_issue_id = p_issue_id;
  insert into public.agent_events (run_id, linear_issue_id, agent, event_type)
  values (p_run_id, p_issue_id, p_agent, 'run.created'),
         (p_run_id, p_issue_id, p_agent, 'lock.acquired'),
         (p_run_id, p_issue_id, p_agent, 'run.started');
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
declare v_agent text;
begin
  if p_status not in ('completed', 'failed', 'blocked', 'canceled') then
    raise exception 'Invalid final status';
  end if;
  select agent into v_agent from public.agent_execution_locks
  where linear_issue_id = p_issue_id and run_id = p_run_id for update;
  if not found then return false; end if;
  update public.agent_runs set
    status = p_status,
    finished_at = pg_catalog.now(),
    commit_sha = p_fields->>'commit_sha',
    pull_request_url = p_fields->>'pull_request_url',
    railway_deployment_id = p_fields->>'railway_deployment_id',
    tests_status = p_fields->>'tests_status',
    review_status = p_fields->>'review_status',
    error = p_fields->>'error'
  where run_id = p_run_id and linear_issue_id = p_issue_id
    and status in ('running', 'reviewing');
  if not found then return false; end if;
  insert into public.agent_events (run_id, linear_issue_id, agent, event_type)
  values (p_run_id, p_issue_id, v_agent, 'run.' || p_status),
         (p_run_id, p_issue_id, v_agent, 'lock.released');
  delete from public.agent_execution_locks
  where linear_issue_id = p_issue_id and run_id = p_run_id;
  return true;
end;
$$;

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
  insert into public.agent_events (run_id, linear_issue_id, agent, event_type,
    payload)
  values (v_lock.run_id, p_issue_id, v_lock.agent, 'lock.expired',
          pg_catalog.jsonb_build_object('expired_at', v_lock.expires_at)),
         (v_lock.run_id, p_issue_id, v_lock.agent, 'lock.recovered', '{}'::jsonb);
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
commit;
