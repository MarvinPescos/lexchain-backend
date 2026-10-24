# Issue #21 — Guarantee recovery of finalized PDF bytes independently of the active copy

GitHub: https://github.com/MarvinPescos/lexchain-backend/issues/21
Label: enhancement
Implemented in: `363f085` feat(snapshot): refuse to anchor without a verified, write-once archive
Builds on: `b164f62` feat(snapshot): archive the finalized PDF and verify archives against the anchor

## Problem

Finalize recorded a `DocumentSnapshot` (`signed_copy_id`, `file_sha256`, `size_bytes`) and anchored the hash, but archiving was best-effort: a storage failure left `backup_key` NULL and finalize still anchored. The verifier could detect a mismatch, but restore had no guaranteed, independently held copy of the anchored bytes.

## What changed

### Finalize (`documents/workflow/finalization.py`)
- Archives the frozen PDF **before** anchoring.
- The archive is read back and checked against the snapshot's SHA-256 **and** size before `backup_key` / `backed_up_at` are set.
- A failed write or read-back raises `ServiceUnavailableError` (503). The request rolls back: no snapshot row, no chain record, document not finalized.
- With `BACKUP_ENABLED=false` finalize still anchors without an archive; `backed_up_at` stays NULL (not recoverable).

### Archive (`snapshot/backups.py`)
- `_matches(snapshot, data)` — one hash + size check used by archive, verify and repair.
- **Write-once:** `archive()` calls `exists()` first. An existing object is reused if it checks out and is never overwritten if it doesn't. Key stays content-addressed: `{document_id}/{file_sha256}.pdf`.
- `verify()` now reports a size mismatch as `mismatch`.
- **Repair of legacy snapshots** (NULL `backup_key`) requires:
  1. a chain reader — `SnapshotArchive(session, backup, chain)`; without one repair fails closed (`unrecoverable`);
  2. the snapshot's `file_sha256` to equal the on-chain hash;
  3. the live signed copy to match the snapshot.
- A bad (mismatching) archive is reported, never replaced.

### Storage (`infrastructure/backup/supabase_backup.py`)
- Upload uses `x-upsert: false`.

### Admin (`features/admin/`)
- `get_snapshot_archive` injects the blockchain provider.
- `POST /admin/backups/verify` description updated to the new repair rules.

### Contract (`snapshot/schemas.py`)
- `DocumentSnapshotResponse.backed_up_at` — when the bytes were archived and verified; null means no recoverable archive. `backup_key` is not exposed.

### Docs
- `CODEBASE.md` §3.10 step 4 rewritten for archive-before-anchor.

## Acceptance criteria

| Criterion | Status |
|---|---|
| Stable private reference to independently recoverable bytes | Done — `backup_key` in a separate backup bucket |
| Backup re-read; SHA-256 and size checked | Done |
| Backup failure blocks finalize; partial work cleaned up / retryable | Done — rollback; retry reuses a verified existing object |
| Altering/deleting the active copy doesn't change the backup | Done (separate bucket, write-once) |
| Legacy docs recoverable only when verified against the on-chain hash | Done — repair checks the chain |
| Backup private, only via authenticated backend workflows | Partial — see Open items |
| Tests | Done |
| Contracts updated | Done — `backed_up_at` |

## Tests (`tests/features/snapshot/test_backups.py`)

`FakeBackup` is now write-once (raises on an existing key) and has a `corrupt` flag for bad read-backs.

- `test_finalize_archives_the_frozen_copy`
- `test_archive_failure_blocks_finalize[down|corrupt]`
- `test_finalize_can_be_retried_after_an_archive_outage`
- `test_retry_after_a_failed_anchor_reuses_the_archive`
- `test_finalize_never_overwrites_a_bad_existing_archive`
- `test_archive_survives_changes_to_the_active_copy[alter|delete]`
- `test_verify_reports_ok_missing_and_mismatch`
- `test_verify_reports_an_archive_of_the_wrong_size`
- `test_repair_rearchives_a_legacy_document_from_the_live_copy`
- `test_repair_refuses_a_live_copy_that_changed`
- `test_repair_does_not_trust_an_archive_that_reads_back_wrong`
- `test_repair_requires_the_snapshot_to_match_the_chain`
- `test_repair_without_a_chain_reader_fails_closed`

Full suite: 190 passed.

## Behaviour change to note

A finalize whose archive was written corrupt is blocked until that object is removed by hand — the bucket refuses overwrites, so retries can't fix it. Repair likewise no longer replaces a mismatching archive from the live copy; it reports it.

## Open items

- `BACKUP_ENABLED=false` still allows finalize without an archive.
- `POST /admin/backups/verify?repair=true` is gated by `require_lawyer` only, not per-document authorization (pre-existing).
- The Supabase backup bucket is in the same project as the live files — it survives accidental deletion/overwrite, not a compromised account. Target: object-locked bucket in a separate account, swapped in via `BACKUP_PROVIDER`.
- `test_archive_survives_changes_to_the_active_copy` runs against in-memory fakes; it can't prove provider-level independence.
- Pre-existing mypy errors in `backups.py` (`BackupCheck.status` literal, optional `SignedCopy`).
- Restore endpoint (depends on this issue) not yet built.
