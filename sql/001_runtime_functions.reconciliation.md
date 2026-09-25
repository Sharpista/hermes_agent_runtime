# SQL reconciliation notes

This package SQL was reconciled against the Supabase-applied artifact from LoLCoach Spec 009 without reapplying or changing the shared database.

Inputs:

- Previous canonical package SQL hash: `684ba8a9b470fc3e9b6893d9796c589d9ae9773f9dfd43491c2983f89f0051ca`.
- Applied artifact: `specs/009-hermes-runtime/migration/001_runtime_functions.proposed.sql`, hash `2cdb809da5e4e27f8bf0bf4be18090625c2f1886c3fa2f35e744e110fa5bc9c5`, 9502 bytes.
- Updated canonical package SQL hash: `2d9f2c368c52c8c21d0b805791b3450cdc9e183587b9de1820b0f86ce6992fd5`, 8022 bytes.

Reconciliation applied:

- A1 kept: `DROP FUNCTION IF EXISTS` preamble for the four RPCs.
- A2 kept: `agent_runs` and `agent_execution_locks` inserts are in the same `BEGIN/EXCEPTION` block.
- A3 removed: the conflict subtransaction rolls back the attempted insert, so the canonical package does not create/update a synthetic failed rejected run.
- A4 kept: sequence grants for `agent_events_id_seq` are hardened for `service_role` only.

Reproduce the diff from this repository clone:

```bash
sha256sum sql/001_runtime_functions.sql
sha256sum ../lols/specs/009-hermes-runtime/migration/001_runtime_functions.proposed.sql
diff -u ../lols/specs/009-hermes-runtime/migration/001_runtime_functions.proposed.sql sql/001_runtime_functions.sql
```

Expected functional diff versus the applied artifact: comments/header differ and A3-only statements (`v_created`, its assignment, and the rejected-run `update ... error = 'RunConflict'`) are absent from the canonical package SQL. The shared Supabase migration artifact and database were not modified by this reconciliation.

## LOL-61 revision — structured event payloads (Spec 009 section 6.4)

- Canonical package SQL hash moved from `2d9f2c368c52c8c21d0b805791b3450cdc9e183587b9de1820b0f86ce6992fd5` (8022 bytes) to `bf752895693990812933e026a260fde78db3f56d956b0d1777162087ab365537` (10283 bytes).
- Functional delta:

| Function | Change |
|---|---|
| `hermes_claim_run` | JSON object payloads for `run.created` (`{}`), `lock.acquired` (`ttl_seconds`, `expires_at`) and `run.started` (`risk`, `execution_mode`, `environment`); lock expiry is computed once into `v_expires_at` and reused by the audit payload. |
| `hermes_finish_run` | `p_fields` is coerced/validated as a JSON object before any write; the per-status allowlisted payload is written for `run.<status>` (`jsonb_strip_nulls`, sanitized short error fallback) and `lock.released` carries `finished_status`. |
| `hermes_recover_expired_lock` | `lock.expired` adds `last_heartbeat_at`; `lock.recovered` carries `previous_run_id`. |

- The A1–A4 reconciliation decisions and their comments (advisory-lock serialization, single exception scope, A3 removal, sequence grant hardening, blocked-run operator runbook) are preserved unchanged; this revision only adds payload lines.
- The applied Supabase artifact is **not** changed by this revision: it stays `2cdb809da5e4e27f8bf0bf4be18090625c2f1886c3fa2f35e744e110fa5bc9c5` (Spec 009 `migration/001_runtime_functions.proposed.sql`, applied in run 117). Applying this revision to the shared project requires a new authorization from `devops`.

Verified on this revision (PostgreSQL 16 container, `postgres:16-alpine`, port 55433): schema reset + migration applied twice (re-appliable), full smoke of claim/conflict/finish/recovery/late-finish/legacy-null reads, and `anon`/`authenticated` EXECUTE denied with `service_role` allowed.
