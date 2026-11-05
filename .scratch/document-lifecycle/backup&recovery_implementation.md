# LexChain — Backup & Recovery Implementation Plan

## 1. Goal

Make the backup survive a compromise of the application server or database, so a tampered or deleted document can always be **detected** (blockchain) and **restored** (backup).

**Threat model**

- **Trusted:** the law firm (Lawyer/Document Issuer) as system owner.
- **In scope:** external attackers, including compromise of the application server (EC2), its credentials, the Supabase database, or the live file storage.
- **Out of scope:** the firm itself tampering with documents; compromise of the owner's own AWS admin credentials; tampering before a document is anchored.

**Core rule:** backups live outside the security boundary of the system they protect. The server gets keys that can only *add*. Keys that can *change or delete* stay with the owner, off the server.

---

## 2. Target architecture

| Where | Holds | Access |
|---|---|---|
| **Supabase** | Auth, database, live PDFs (Storage) | Server (service key) |
| **AWS app account** | EC2 boxes (API, Celery workers, Marker) | Instance role. Developers: scoped access to this account only |
| **AWS backups account** | Docs bucket (original finalized PDFs); DB bucket (hourly dumps); monthly DB bucket; RDS copy of the app tables with AWS Backup | App instance role through the bucket policy: docs read + add, dumps add only, no RDS access. Lawyer: admin + MFA. No developer access |
| **Base** | Document SHA-256 hashes | Server holds a recorder key only; the owner key stays offline |

Production buckets use **S3 Object Lock in Compliance mode (WORM)**: nobody can delete or overwrite a locked object version until its retention expires, including account admins. Until real documents go in, buckets stay on GOVERNANCE with 1-day retention, as ADR-0001 requires, because Compliance can't be undone.

LexChain is validated, not deployed. Validation keeps those bucket defaults, runs paid resources only while testing, and tears them down afterward. The paper describes the production design and states which parts were tested.

Two accounts keep the backups outside the boundary of the system they protect: nothing in the app account can change the bucket policy, and closing or losing the app account leaves the backups intact.

We use separate buckets because Object Lock's default retention applies to a whole bucket, and documents, hourly dumps and monthly dumps need different periods. Setting retention per object instead would mean giving the server `s3:PutObjectRetention`.

### What an attacker gets from each compromise

| Compromised | Attacker can | Outcome |
|---|---|---|
| Supabase DB | Edit or wipe tables, including snapshot rows | Snapshot edits flagged by `baseline_trusted=false` and the daily check; `SNAPSHOT_COMPROMISED` when the file was changed too; DB restored from the DB bucket |
| Supabase Storage | Tamper with or delete live PDFs | Detected (`TAMPERED`, or `FILE_MISSING` once issue 04 is done); restored from the docs bucket |
| EC2 server | Supabase key, recorder key, limited AWS role | Same as above. Can't delete backups or overwrite anchors; can add junk under new keys (see Limitations) |
| A developer's AWS keys | App account actions only | Equivalent to an EC2 compromise at worst; no access to the backups account |
| Lawyer's AWS admin login | — | Out of scope; Compliance mode still holds until retention expires |

---

## 3. AWS setup

All of this goes through OpenTofu (ADR-0001). Console edits drift from state. `infra/backups` already creates one locked bucket per environment; `infra/app/iam.tf` already grants the instance role.

### 3.1 Accounts
- [x] Two AWS accounts exist.
- [ ] Add local CLI profiles `lexchain` (app) and `lexchain-backups` (backups); `~/.aws/config` only has `[default]`.
- [ ] Enable MFA on both root users; don't use root day-to-day.
- [ ] Admin IAM user for the lawyer in the backups account, with MFA.
- [ ] Developers get users in the app account only.

### 3.2 `infra/backups`
- [ ] Add two more buckets, for hourly and monthly dumps, each with its own `retention_days`, reusing the same settings. Bucket names are global, so keep the unique suffix.
- [ ] Copy `production.tfvars.example`: `aws_profile = "lexchain-backups"`, the real `app_account_id`, `region = "ap-southeast-1"` (the default is `us-east-1`).
- [ ] Docs bucket retention: GOVERNANCE / 1 until real documents go in, then **COMPLIANCE** for the firm's record-retention period. ⚠️ Compliance retention cannot be shortened or removed.
- [ ] Hourly DB bucket retention: **7 days**, same mode rule.
- [ ] Monthly DB bucket retention: the firm's record-retention period, same mode rule. Use 1 day during validation.
- [ ] DB bucket lifecycle, because the bucket is versioned and `Expiration` alone only adds a delete marker:
  - `Expiration` after ~9 days on the hourly bucket.
  - `NoncurrentVersionExpiration` after 1 day, which is what actually removes the dump.
  - Remove expired delete markers.
- [ ] Docs bucket policy: deny `s3:PutObject` when `s3:if-none-match` is null, so nothing can replace an existing key. **Required:** `fetch` reads the newest version, so a junk version would otherwise break restore.
- [ ] DB bucket policies: grant the app account `s3:PutObject` only.

### 3.3 `infra/app/iam.tf`
The role already has no delete rights, and Object Lock blocks deletes regardless, so no explicit deny list is needed.
- [ ] Add `backup_db_bucket_name` and `backup_db_monthly_bucket_name`, and grant `s3:PutObject` on both. No read: restores are manual, from the lawyer's account, so a compromised server can't read past dumps.
- [ ] Keep the existing docs bucket grants, including `s3:GetObjectVersion` and `s3:ListBucket`. Without `ListBucket`, a missing object returns 403 instead of 404, and `exists()` can't tell "missing" from "forbidden".

Cross-account access needs both sides: the bucket policy and the role policy must each allow the action.

---

## 4. File backup code

The `BackupProvider` protocol (`store` / `fetch` / `exists`, no delete) was designed for this swap, so callers don't change.

- [ ] `backend/app/infrastructure/backup/s3_backup.py`: `S3BackupProvider`
  - boto3 client using the default credential chain, which picks up the instance role.
  - Run blocking boto3 calls in `asyncio.to_thread`, like `SupabaseBackupProvider`.
  - `store`: `put_object(Bucket, Key, Body, ContentType, IfNoneMatch="*", ChecksumAlgorithm="SHA256")`. Object Lock rejects a write without a checksum.
  - `fetch`: `get_object(...)["Body"].read()`.
  - `exists`: `head_object`; return `False` on 404 and raise on anything else, so an outage isn't read as a lost archive.
- [ ] `factory.py`: add the `"s3"` branch.
- [ ] `core/config.py`: `BACKUP_PROVIDER=s3`, `BACKUP_BUCKET=<docs bucket>`, `AWS_REGION=ap-southeast-1`.
- [ ] `requirements.txt`: `boto3`.
- [ ] Tests mirroring `tests/features/snapshot/test_backups.py`, using botocore's `Stubber` so no new test dependency is needed.
- [x] **Restore without trusting the snapshot row:** build the key from the on-chain hash (`<document_id>/<onchain_hash>.pdf`) in `_archived_original`, and allow restore from `SNAPSHOT_COMPROMISED`. Today a nulled `backup_key` or an edited snapshot blocks restore even though the good file is in S3.
- [ ] **Stop database edits hiding tampering:** Verify returns `NOT_ANCHORED` from the `lifecycle` column before it reads the chain, so read the chain first (issue 04). Deleted snapshot rows also drop out of the daily check (issue 02).
- [ ] **Move existing archives:** switch the provider, then run `POST /admin/backups/verify?repair=true`. Repair only uses the live copy, so a document already tampered with comes back `unrecoverable`; add a fallback that reads the old Supabase archive. Keep that bucket until every document reports `ok`.
- [x] Scheduled verification reads the chain. An archive the chain can't confirm is reported `unverified`, never `ok`.

**Scope note:** only **finalized (anchored)** documents are archived. Drafts and in-progress files are covered only by the database and Supabase Storage.

---

## 5. Database backup

- [ ] Celery beat task (e.g. `workers/backup_database.py`):
  1. Run `pg_dump` against Supabase, streaming to a temp file rather than memory.
  2. gzip the output.
  3. Upload it to `<DB bucket>/<UTC timestamp>.sql.gz`. The first dump of each month also goes to the monthly bucket.
  4. Log `backup.db.stored` / `backup.db.failed`.
- [ ] Route it to its own queue. `document.*` lands on the `-c 1` documents worker, where a dump and OCR would block each other.
- [ ] Schedule: hourly (`BACKUP_DB_SECONDS`, like `BACKUP_VERIFY_SECONDS`). Beat restarts an interval when its schedule file is lost, so use a crontab schedule if the box is rebuilt often.
- [ ] Config: `BACKUP_DB_BUCKET`, and a plain `postgresql://` URL for `pg_dump`. `DB_URL` uses `postgresql+asyncpg://`, which `pg_dump` rejects.
- [ ] Use Supabase's **session pooler**. The transaction pooler doesn't work with `pg_dump`, and the direct host is IPv6-only by default while the security group only allows IPv4 egress.
- [ ] Worker image: install `postgresql-client` with a version **≥ Supabase's Postgres version** (check with `select version();`), or `pg_dump` refuses to run.
- [ ] Dumps contain personal data (RA 10173) and auth data: they stay private, encrypted at rest (SSE-S3), and readable only from the lawyer's admin account.
- [ ] **Restore drill:** restore one dump into a **fresh Supabase project**, then confirm tables, row counts, and a login. A local Postgres lacks Supabase's `auth` and `storage` roles and schemas, so it doesn't prove recovery. Keep the steps in a short runbook.
- [ ] **Runbook, reconciliation findings:** `POST /admin/chain/reconcile` and the weekly job never repair anything. A `document_missing` finding means the document row was deleted: restore the row from a DB dump. `snapshot_missing` means the same for its snapshot row. `orphaned_anchor` is a warning, not tampering: finalize anchored and then failed to commit, or `lifecycle` was edited, so check the document before acting.
- [ ] State the recovery point: about 5 minutes through RDS, up to 1 hour through dumps. Finalized PDFs and their anchors lose nothing, because both are written at finalize. Documents finalized after the recovery point have an archive and an anchor but no database row: the reconcile job in issue 02 lists them, and the runbook needs a step for them.

### 5.1 Continuous recovery on AWS

AWS point-in-time recovery only works on databases in AWS, so a copy of the app tables runs in RDS. Tampering replicates too: protection comes from restoring to just before the edit.

- [ ] `infra/backups`: RDS Postgres (`db.t4g.micro`) in the backups account, not publicly reachable, behind a variable so it can be destroyed after validation. Same Postgres major version as Supabase, with the `vector` extension.
- [ ] Supabase: a dedicated replication role and a publication for the app tables in `public`. `auth` stays out; dumps cover it.
- [ ] RDS: create the schema from `pg_dump --schema-only --schema=public`, then subscribe.
- [ ] Networking: subscriptions need Supabase's direct connection, not the pooler, and it is IPv6-only by default. Use dual-stack networking on RDS, or Supabase's IPv4 add-on.
- [ ] AWS Backup: a plan with continuous backups for the instance, in a vault locked in **Governance** mode. A Compliance vault lock can't be changed after its grace period. Up to 35 days of recovery; use a short retention during validation.
- [ ] Runbook: every Alembic migration also runs on RDS, because schema changes don't replicate.
- [ ] Runbook: restore RDS to a time just before the edit, as a new instance, then copy `public` back into Supabase.
- [ ] Runbook: if `lifecycle` was edited, verify shows `TAMPERED` but restore refuses. Correct `lifecycle` from a DB backup first, then restore.
- [ ] Alert on replication lag and on a dropped subscription.

### Alerting
- [ ] `backup.db.failed`, `backup.verify.*` problems, a missing hourly dump, and replication lag must reach a person. Logs alone mean a stopped backup goes unnoticed, and a clean dump may age out of the 7-day window before anyone looks. Check for the missing dump from outside the server.

---

## 6. Deployment

- [ ] **Before anything else:** `get_blockchain_provider` returns `MockBlockchainProvider` unless `ENVIRONMENT == "development"`. With `ENVIRONMENT=production`, nothing is anchored and every chain read returns `None`. Fix this before going live.
- [ ] Boxes use the instance role from 3.3.
- [ ] **Contract ownership (C2, deferred):** create an offline owner wallet and call `transferOwnership` to it. The current server key stays a recorder, so anchoring keeps working. The app never calls `correctDocumentHash()`, so it doesn't need the owner key. Rename `CONTRACT_OWNER_PRIVATE_KEY` to match. Do this once per environment's contract.
- [ ] Server environment holds the Supabase keys and the recorder key.
- [ ] **Never on the server:** the contract owner key, or any admin/root login for either AWS account.

---

## 7. Verification & defense demo

| # | Action | Expected |
|---|---|---|
| 1 | Finalize a document | Object appears in the docs bucket as `<document_id>/<sha256>.pdf` |
| 2 | Edit the live PDF in Supabase Storage → Verify | `TAMPERED` + tamper report (diff) |
| 3 | Restore | Archived original re-hashed against chain → becomes current → Verify: `AUTHENTIC` |
| 4a | Edit only the snapshot row → Verify | `AUTHENTIC` with `baseline_trusted: false` (the file is genuine); the daily check reports `mismatch` |
| 4b | Edit the snapshot row and the live PDF → Verify | `SNAPSHOT_COMPROMISED` |
| 5 | From EC2: `aws s3 rm s3://<docs bucket>/<key>` | `AccessDenied` (the role has no delete grant) |
| 6 | From EC2: upload over an existing key without `If-None-Match` | `AccessDenied` (bucket policy) |
| 7 | From the lawyer's admin account: upload one test object with explicit COMPLIANCE retention of 1 day, then `aws s3api delete-object --version-id <id>` on it | Blocked (Object Lock, Compliance). Without a version id, S3 just adds a delete marker. The bucket default stays GOVERNANCE, as ADR-0001 requires |
| 8 | Run the DB backup task | `.sql.gz` appears in the DB bucket |
| 9 | From EC2: download a dump | `AccessDenied` |
| 10 | Restore drill | Dump loads into a fresh Supabase project; record the time taken |
| 11 | Write marked rows, tamper with one, restore RDS to just before the edit | Tampered value gone; record the data lost and the time taken |

Every tampering attack and its test is listed in `issues/03-tampering-attacks-and-database-recovery.md`.

---

## 8. Paper updates

- [ ] Add the **threat model** paragraph (section 1).
- [ ] p. 9: the backup repository is stored in a separate AWS account under Object Lock in Compliance mode (WORM). The Lawyer/Document Issuer may initiate backups and restore files but cannot alter or remove stored versions. The application server holds only a least-privilege credential.
- [ ] p. 10: recovery no longer depends on "backup administration by the Lawyer"; it is enforced by Object Lock and IAM.
- [ ] p. 72, Functional Requirement 9: add WORM storage, least-privilege server access, and database backups.
- [ ] Table 31, Backup and Recovery row: separate-account immutable backups for documents and the database; scheduled integrity verification against on-chain hashes; restore drill.
- [ ] **Limitations:**
  - Tampering by the firm itself, compromise of the owner's AWS admin credentials or owner wallet, and tampering before anchoring are out of scope.
  - Only finalized documents are archived.
  - Database recovery can lose about 5 minutes through RDS, or up to 1 hour through dumps if replication is lost. Report the measured figures.
  - Metadata, derived data, audit history and access changes are recoverable but not detected.
  - A compromised server can add junk objects under new keys, and Compliance retention means they can't be removed until it expires.
  - Compliance retention blocks RA 10173 erasure requests during that period.
  - Verify runs on the server, so its results can't be trusted while the server itself is compromised.
- [ ] **Supporting concepts to cite:** 3-2-1 backup rule; least privilege; ISO/IEC 27001:2022 A.8.13 (Information backup) and A.5.15 (Access control).

**Suggested wording (threat model):**

> The law firm, as system owner, is assumed trusted. LEXCHAIN defends against unauthorized modification by external attackers, including compromise of the application server, its credentials, and the database. Original documents and database dumps are backed up to a separate cloud account, alongside a continuously backed-up copy of the database, under S3 Object Lock in compliance mode, a Write-Once-Read-Many (WORM) storage model in which no user, including account administrators, can modify or delete a stored version before its retention period expires. The application server holds only a least-privilege credential that can add backups and read archived documents but cannot delete or overwrite them, and it can record new hashes on the Base blockchain but cannot change an anchored one. Integrity is verified against those anchored hashes, and a tampered document is restored from the backup only after the backup is re-hashed and matched against the on-chain record.

This wording is only true once the bucket policy in 3.2 and the ownership transfer in section 6 are done.
