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
