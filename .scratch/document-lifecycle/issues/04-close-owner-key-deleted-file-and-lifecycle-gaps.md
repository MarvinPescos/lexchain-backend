# Close the deleted file and lifecycle gaps

Status: resolved
Type: task

Attack IDs (F4, R4) refer to `03-tampering-attacks-and-database-recovery.md`.

## Problem

Two attacks defeat verification today:

- **F4, deleted live PDF:** a deleted file is reported as `VERIFICATION_UNAVAILABLE`, which says it isn't a tamper result, and restore refuses to run.
- **R4, edited `lifecycle`:** verify returns `NOT_ANCHORED` before it reads the chain, so changing `lifecycle` hides a tampered file.

## Order

Both parts go in one branch, because both change `_verify` in `blockchain/service.py`. Write the tests first.

Issue 01 builds on both parts.

## Part 1: report and restore a deleted file (F4)

Confirmed against dev Supabase on 2026-11-05: a missing object raises `StorageApiError` with `code='not_found'` and `status='404'`. `status` is a string, so match on `code`.

- [ ] Add `VERIFY_FILE_MISSING = "FILE_MISSING"` to `blockchain/constants.py` and to `VERIFICATION_STATUSES`.
- [ ] In `_verify`, catch `code == "not_found"` before the outage handler. Return `FILE_MISSING` with `is_authentic=False`. Keep `VERIFICATION_UNAVAILABLE` for real outages.
- [ ] If the snapshot also disagrees with the chain, return `SNAPSHOT_COMPROMISED`, as for a changed file.
- [ ] Add `file_missing` to `IntegrityReportSummary` and to the query that fills it.
- [ ] Let `documents/workflow/restoration.py` accept `FILE_MISSING`:
  - There is no `current_hash`, so the `assert verdict.current_hash is not None` must allow it.
  - Skip `_current_bytes`; there is nothing to preserve. `preserved_copy_id` stays null.
  - `change.from_hash` and `previous_hash` stay null. Both are already nullable.
- [ ] Update the `status` descriptions in `blockchain/schemas.py`, and the `/verify` and `/restore` route descriptions.
- [ ] Add `FILE_MISSING` to `docs/frontend-verify-screen.md`: red, with a restore button and no diff.

Issue 01's scheduled check must treat `FILE_MISSING` as a tamper finding.

## Part 2: read the chain before `lifecycle` (R4)

- [ ] In `_verify`, read the chain first:
  - Record exists: run the hash check whatever `lifecycle` says. If `lifecycle` isn't `FINALIZED`, log `blockchain.verify.lifecycle_mismatch`.
  - No record and not `FINALIZED`: return `NOT_ANCHORED`.
  - No record and `FINALIZED`: raise `NotFoundError`, as now.
- [ ] Add a way to skip the empty-read retries in `get_document_hash`, and use it when `lifecycle` isn't `FINALIZED`. It retries 3 times, 2 seconds apart, so every draft would otherwise take about 6 seconds. Update `BlockchainProvider` and both providers.
- [ ] Replace `assert copy is not None`. An orphaned anchor from a failed finalize can have a record and no finalized state. Return a result instead of crashing.
- [ ] A missing snapshot with a chain record raises `NotFoundError` today. Keep that: issue 02 reports these.
- [ ] `get_document_hash` raises `ServiceUnavailableError` when the chain can't be reached, which drafts never hit before. Catch it and return `VERIFICATION_UNAVAILABLE`.

Restore stays limited to `FINALIZED`. After this attack, verify shows `TAMPERED` but restore refuses, so add a runbook step to correct `lifecycle` from a DB backup first.

## Acceptance criteria

**Part 1**
- [ ] A deleted live file returns `FILE_MISSING`.
- [ ] A storage outage still returns `VERIFICATION_UNAVAILABLE`.
- [ ] Restore brings back a deleted file, and verify then returns `AUTHENTIC`.
- [ ] The restore's history row has a null `from_hash` and the anchored `to_hash`.
- [ ] A deleted file with an altered snapshot returns `SNAPSHOT_COMPROMISED`.
- [ ] The integrity report counts `file_missing`.

**Part 2**
- [ ] A tampered file with an edited `lifecycle` returns `TAMPERED`, not `NOT_ANCHORED`.
- [ ] An authentic file with an edited `lifecycle` returns `AUTHENTIC` and logs the mismatch.
- [ ] A real draft returns `NOT_ANCHORED` with no retry delay.
- [ ] A chain outage on a draft returns `VERIFICATION_UNAVAILABLE`, not a 503.
- [ ] An orphaned anchor returns a result, not a crash.
- [ ] Existing tests still pass, with pytest `TEST_DB_URL` on port 5436.

## Out of scope

- Restoring when `lifecycle` was edited. It stays a manual step.
- Restore from the on-chain hash when the snapshot row is altered (plan section 4).

## Comments

2026-11-05: merged in #50. The owner key gap (C2) was left out of this issue and is deferred; see issue 03.
