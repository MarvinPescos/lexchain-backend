# Tampering attacks: add database recovery and validate

Status: ready-for-human
Type: task
Blocked by: 01, 02, 04

`ready-for-human` because the AWS accounts, Supabase settings and the panel demo need a person.

## Attacks and coverage

Each attack comes from the threat model in `../backup&recovery_implementation.md`: an external attacker with access to the application server, its credentials, the database or file storage. The firm is trusted.

**Status**
- **Covered:** works in the code today
- **Built:** code and infra written, but not applied or validated yet
- **Planned:** tracked, not built
- **Gap:** open
- **Accepted:** known limitation, stated in the paper
- **Out of scope:** excluded by the threat model

"Plan" below means `backup&recovery_implementation.md`.

### Live files (Supabase Storage)

| # | Attack | Detected by | Recovered by | Status | Tracked in |
|---|---|---|---|---|---|
| F1 | Edit a finalized PDF | Verify on open and the scheduled live check (`TAMPERED`), with the OCR diff on request | Restore from archive | **Covered**. The archive stays in the same Supabase project until the S3 provider is switched on | 01; plan 4 (S3) |
| F2 | Replace a seal or signature without changing text | Verify (`TAMPERED`). The diff can't locate it | Restore from archive | **Covered** | None |
| F3 | Repoint `signed_copy_id` or `storage_url` at another file | Verify hashes whatever the row points at, so `TAMPERED` | Restore from archive | **Covered** | None |
| F4 | Delete a finalized PDF | Verify and the scheduled live check (`FILE_MISSING`) | Restore from archive | **Covered** | 04 |

### Archive (backup bucket)

| # | Attack | Detected by | Recovered by | Status | Tracked in |
|---|---|---|---|---|---|
| A1 | Edit or delete an archived PDF | Scheduled check (`mismatch`, `missing`) | Blocked by Object Lock once on S3 | **Built**. Detected only until S3 is switched on, since the Supabase bucket has no lock | Plan 3, 4 |
| A2 | Upload a new version over an archive key | Nothing yet | Bucket policy denies it | **Built** | Plan 3.2 |
| A3 | Fill the bucket with junk under new keys | Not detected | Removed after retention expires | **Accepted** | Plan 8 |

### Integrity records (database)

| # | Attack | Detected by | Recovered by | Status | Tracked in |
|---|---|---|---|---|---|
| R1 | Edit the snapshot hash only | Verify (`baseline_trusted=false`) and the scheduled check | DB backup | **Covered** for detection; recovery **Built** | This issue |
| R2 | Edit the snapshot hash and the PDF | Verify (`SNAPSHOT_COMPROMISED`) | Restore from archive by the on-chain hash; the row itself needs a DB backup | **Covered** on `feat/restore-from-chain`, not merged | Plan 4 (restore from chain) |
| R3 | Null `backup_key` | Scheduled check (`missing`) | Restore from archive by the on-chain hash; the row itself needs a DB backup | **Covered** on `feat/restore-from-chain`, not merged | Plan 4 (restore from chain) |
| R4 | Change `lifecycle` away from `FINALIZED` to hide a tampered PDF | Verify reads the chain first, so `TAMPERED` | Correct `lifecycle` from a DB backup, then restore | **Covered** | 04 |
| R5 | Delete a snapshot row | Reconcile job (`snapshot_missing`); verify returns 404 | DB backup | **Covered** for detection; recovery **Built** | 02 |
| R6 | Delete a document row | Reconcile job (`document_missing`) | DB backup | **Covered** for detection; recovery **Built** | 02 |
| R7 | Wipe or encrypt the database | Outage | DB backup | **Built** | This issue |

### Other database content

| # | Attack | Detected by | Recovered by | Status | Tracked in |
|---|---|---|---|---|---|
| D1 | Edit metadata (`file_name`, book, parties, `finalized_by`) | Not detected | DB backup, if noticed in time | **Accepted** | `../tamper-coverage.md` |
| D2 | Edit OCR text, chunks, embeddings or insights to change RAG answers | Not detected | Regenerate from the verified PDF | **Accepted** | `../tamper-coverage.md` |
| D3 | Rewrite audit or verification history | Not detected | DB backup, if noticed in time | **Accepted** | `../tamper-coverage.md` |
| D4 | Edit roles or party access | Not detected | DB backup, if noticed in time | **Accepted** | `../tamper-coverage.md` |
| D5 | Edit a row, then wait until every backup contains the edit | Not detected | Monthly dumps | **Built** | This issue |

### Chain

| # | Attack | Detected by | Recovered by | Status | Tracked in |
|---|---|---|---|---|---|
| C1 | Overwrite an anchor with the recorder key | Blocked: `DocumentAlreadyRecorded` | Not needed | **Covered** | None |
| C2 | Replace an anchor with the owner key, which is on the server | Not by the app | Not through the app | **Gap**, deferred. Fix: plan's contract ownership step | Plan 6 |
| C3 | Anchor a fake document with the stolen recorder key | Issue 02 flags it as `document_missing`, unless the attacker also adds a matching row | Not applicable | **Accepted** | Paper limitation |

### Backups

| # | Attack | Detected by | Recovered by | Status | Tracked in |
|---|---|---|---|---|---|
| B1 | Delete or read dumps from the server | Role is add-only | Blocked by IAM and Object Lock | **Built** | Plan 3 |
| B2 | Stop backups or replication unnoticed | Alerts | Fix, then re-sync | **Built** | Plan 5, this issue |
| B3 | Tamper with data that replicates to RDS | Same as the original attack | Restore RDS to just before the edit | **Built** | This issue |

### Out of scope

| # | Attack | Note |
|---|---|---|
| O1 | Tampering by the firm itself | Firm is trusted |
| O2 | Compromise of the owner's AWS admin login or owner wallet | Compliance locks still hold until retention expires |
| O3 | Tampering before anchoring | Finalize checks the file against the upload, but can't tell if the upload was correct. A review prompt before finalize would reduce this |
| O4 | Compromised server returning false verdicts | Verify runs on the server. Anyone can re-hash a PDF and read the chain independently |

## Scope

### Database recovery

Targets: about 5 minutes of data loss through RDS, and 1 hour through dumps if replication is lost.

| Layer | Worst-case loss | Kept for | Covers |
|---|---|---|---|
| RDS copy with AWS Backup continuous backups | About 5 minutes plus replication lag | Up to 35 days | App tables |
| Hourly `pg_dump` to locked S3 | 1 hour | 7 days | Everything, including `auth` |
| Monthly `pg_dump` to locked S3 | 1 month | The firm's retention period | Everything, including `auth`. For tampering noticed late (D5) |

- [ ] Follow the plan's sections 3.2 and 5 for dumps and buckets.
- [ ] Follow section 5.1 for RDS and AWS Backup.

### Before validation

- [ ] Check both accounts' plans and credit balances in the Billing console.
- [ ] Use the real chain: `get_blockchain_provider` returns the mock unless `ENVIRONMENT == "development"`.
- [x] Finish issue 04.
- [ ] Work through plan section 4: S3 provider merged in #52; restore from chain is on `feat/restore-from-chain`, not merged.

## Validation

Run each attack for real and keep the evidence.

| Attacks | Test | Evidence |
|---|---|---|
| F1, F2, F3 | Tamper, open the document, view the diff, restore | Screenshots, hash matching the chain |
| F4 | Delete the PDF, verify, restore | The new verdict, hash matching the chain |
| A1, A2 | Run plan section 7 rows 5, 6 and 7 | `AccessDenied` output |
| R1, R2, R3 | Edit the rows, verify, restore | Verdicts and a restored document |
| R4 | Change `lifecycle` on a tampered document, verify | `TAMPERED`, not `NOT_ANCHORED` |
| R5, R6 | Delete the rows, run the reconcile job | `snapshot_missing`, `document_missing` |
| R7, B3 | Write marked rows, tamper with one, restore RDS to just before the edit | Rows that survived, measured data loss and restore time |
| R7 | Load a dump into a fresh Supabase project, confirm a login | Measured restore time |
| C1 | Call `storeDocumentHash` on an anchored ID | Reverted transaction |
| B1 | Run plan section 7 row 9 | `AccessDenied` output |
| B2 | Stop the dump task and the subscription | Alert received |

## After validation

- [ ] Delete the RDS instance and the AWS Backup plan.
- [ ] Check that nothing else is billing in either account.
- [ ] Record the measured data loss and restore times in the paper.

## Comments

2026-11-05: dumps, RDS infra, runbook and `S3BackupProvider` merged in #52 but not applied, so the two recovery checkboxes above stay open. Issues 01, 02 and 04 are resolved. C2 is deferred. What remains is the human work: apply the infra, run validation, tear down.
