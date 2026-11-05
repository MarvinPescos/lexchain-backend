# Verify on open, with a scheduled live file check as fallback

Status: resolved
Type: task
Blocked by: 04

## Problem

Tampering with a finalized PDF in Supabase Storage is only detected when someone calls `GET /documents/{id}/verify`. The scheduled `verify_backups` job checks archives, snapshots and the chain, but never reads the live signed copy. A tampered file can be opened and downloaded with no warning, and its findings only reach the logs.

## Design

1. **On open:** the frontend runs a quick hash check alongside loading the PDF and shows a badge or banner. No OCR.
2. **On request:** "See what changed" calls the existing full verify, which runs the OCR diff.
3. **Fallback:** the scheduled job also checks every live file against the chain, so documents nobody opens are still covered, and alerts the lawyer on a mismatch.

## Scope

### Quick check

- Add a `report: bool = True` query param to `GET /{document_id}/verify` in `documents/router.py`. `report=false` passes `tamper_report=False`.
- Default behaviour stays the same: full report, every verdict recorded.
- With `report=false`, record the verdict in `verification_results` only when it is not `AUTHENTIC`. Opens must not flood the integrity report.

### Scheduled live check

- Add the check to `SnapshotArchive` in `snapshot/backups.py`, called from `verify()`. Do not reuse `_verify`: it requires an authenticated user for `DocumentAccess`.
- Hash the document's **current** signed copy (`document.signed_copy_id`), not `snapshot.signed_copy_id`. They differ after a restore.
- Compare against the on-chain hash, never the snapshot row.
- Download directly from storage, never through the CDN.
- Run it regardless of the archive's status, so a bad archive doesn't skip the live check.
- If storage or the chain can't be read, the result is unknown: log it, and don't record or alert a tamper.
- A live file that no longer exists is a tamper finding, not an unknown result (`FILE_MISSING`, issue 04).
- Add a live-file count and problems to `BackupVerificationResponse`, kept separate from the archive statuses. `POST /admin/backups/verify` then reports them too.
- Interval stays `BACKUP_VERIFY_SECONDS`. No per-document `last_verified_at`: a database attacker could keep updating it so a document is never checked.

### Alerts

- On a live mismatch, write a `verification_results` row with `verified_by=NULL` (the column is already nullable) and create a notification for `document.finalized_by`.
- Add a `NotificationType` for this. If the column is a native Postgres enum, this needs a migration.
- Alert once per tampered file: skip when the latest `verification_results` row for the document already has the same `current_hash` and a non-`AUTHENTIC` status. The job runs daily and must not repeat the alert.

### Docs

Update `docs/frontend-verify-screen.md`:
- Document `?report=false` and the open flow: non-blocking call, badge or banner, "See what changed" for the full report.
- `AUTHENTIC` shows a badge. `TAMPERED` and `SNAPSHOT_COMPROMISED` show a red banner. `VERIFICATION_UNAVAILABLE` shows amber and never blocks reading.
- Call it for every document, and show nothing for `NOT_ANCHORED`. Don't skip it based on `lifecycle`: that value comes from the database, so an edited one would hide the check. This relies on verify reading the chain first (issue 04).
- A 404 on a finalized document (no chain record) shows a warning, not a generic error.
- Keep the download button disabled until the check returns.
- Fix the stale parts: the "What you cannot show yet" section, and `VERIFICATION_UNAVAILABLE` being blamed on OCR. It is caused by a storage read failure.

## Acceptance criteria

- [ ] `report=false` on a tampered document returns `TAMPERED` without calling OCR, and `tamper_report` is absent.
- [ ] `report=false` on an authentic document writes no `verification_results` row.
- [ ] `report=false` on a tampered document writes one row.
- [ ] `GET /verify` without the param behaves as before.
- [ ] The scheduled job flags a live file that differs from the chain, even when the archive is `ok`.
- [ ] After a restore, the job checks the restored copy and reports it clean.
- [ ] A storage or chain read failure produces no tamper row and no alert.
- [ ] A tampered file produces one notification to `finalized_by` across repeated runs. A new tampered hash alerts again.
- [ ] Tests added in `tests/features/snapshot/test_backups.py` and `tests/features/blockchain/`. Run pytest with `TEST_DB_URL` on port 5436.

## Out of scope

- `S3BackupProvider` and restore from the on-chain hash when the snapshot row is altered (see `backup&recovery_implementation.md`).
- Frontend implementation.
- Caching `AUTHENTIC` results. Add only if storage egress becomes a problem.
- Detecting snapshot rows deleted directly from the database.

## Comments

2026-11-05: merged in #53.
