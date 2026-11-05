# Reconcile the database against the chain

Status: resolved
Type: task
Blocked by: 01

## Problem

Every integrity check starts from the rows in the database. `SnapshotArchive.verify()` loops over `document_snapshots`, so a document or snapshot row deleted directly in the database is never checked, and nothing reports it missing. Only the chain knows what should exist.

See `../tamper-coverage.md` for where this fits.

## Design

A scheduled job lists every document ID anchored on-chain and compares that list with the database. It checks presence only: existing checks already compare hashes.

The contract supports this as deployed. `DocumentRecordVerified` carries `documentId` as a plain, non-indexed string in the event data, so it can be decoded from logs. No contract change or redeploy.

## Scope

### Provider

- Add `list_anchored_document_ids(from_block: int) -> set[str]` to `BlockchainProvider` in `infrastructure/blockchain/base.py`, and implement it in `web3_provider.py` and `mock_provider.py`.
- Read `DocumentRecordVerified` logs from `from_block` to the latest block, in chunks of `CHAIN_LOG_CHUNK_BLOCKS`. RPC providers cap the `get_logs` range; confirm the cap for the `WEB_RPC_URL` provider and set the default under it.
- A failed chunk fails the whole scan. A partial list must never be reported as a complete one.

### Reconciliation

Add `workers/reconcile_chain.py`, following `workers/verify_backups.py`, plus a `document.reconcile_chain` Celery task and a beat entry.

| Anchored ID in the database | Status | Meaning |
|---|---|---|
| Document `FINALIZED` with a snapshot | `ok` | Nothing to report |
| Document `FINALIZED`, no snapshot | `snapshot_missing` | Snapshot row deleted |
| Document exists, not `FINALIZED` | `orphaned_anchor` | Finalize anchored and then failed to commit, or the lifecycle was edited. Warn, don't report as tampering |
| No document row | `document_missing` | Document row deleted |

`record_document` runs before finalize's commit, so `orphaned_anchor` can occur without any attack. Keep it separate so it doesn't raise false alarms.

The reverse case, a `FINALIZED` document with no chain record, is already reported as `unverified` by `SnapshotArchive._check` when its snapshot exists. Don't duplicate it here.

### Config

- `CHAIN_SCAN_FROM_BLOCK`: the contract's deployment block. Required for the job to run; without it, log an error and skip rather than scan from block 0.
- `CHAIN_LOG_CHUNK_BLOCKS`
- `CHAIN_RECONCILE_SECONDS`: default weekly. Each run rescans the full history, and a stored cursor would be one more database value an attacker could move.

Every environment must use its own contract. If dev and production share one, dev anchors show up as `document_missing`.

### Alerts

- Log every finding as `chain.reconcile.*`.
- For `snapshot_missing` and `document_missing`, notify every user with `ROLE_LAWYER`. `finalized_by` can't be used because the row may be gone. Reuse the `NotificationType` from issue 01, or add one if the wording doesn't fit.
- Alert once per document ID per finding. There is no `verification_results` row to deduplicate on when the document is gone (its FK requires one), so check existing notifications through `event_metadata`.

### Admin

Add `POST /admin/chain/reconcile`, following `POST /admin/backups/verify`, returning counts and problems.

### Docs

Update `backup&recovery_implementation.md` so the restore runbook says a `document_missing` finding means restoring the row from a DB dump.

## Acceptance criteria

- [ ] A deleted document row is reported as `document_missing`.
- [ ] A deleted snapshot row is reported as `snapshot_missing`.
- [ ] An anchored document that isn't `FINALIZED` is reported as `orphaned_anchor` and sends no notification.
- [ ] A clean database reports no problems.
- [ ] A failed log chunk aborts the run with no findings or alerts.
- [ ] Scans cross chunk boundaries with no gaps or duplicates.
- [ ] Repeated runs alert once per finding.
- [ ] A missing `CHAIN_SCAN_FROM_BLOCK` skips the run and logs an error.
- [ ] Tests run against the mock provider, with pytest `TEST_DB_URL` on port 5436.

## Out of scope

- Automatic repair. Restoring a row is a manual step from a DB dump.
- Metadata, chunk and audit log tampering (see `../tamper-coverage.md`).
- Documents anchored on a previous contract deployment. If one exists, its address and block need to be scanned too.

## Comments

2026-11-05: merged in #51.
