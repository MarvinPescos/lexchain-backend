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
| **AWS** (1 account) | EC2 (backend + Celery workers); `lexchain-backup-docs` (original finalized PDFs); `lexchain-backup-db` (database dumps) | Server: EC2 IAM role, **upload + read only**. Developers: EC2 permissions only. Lawyer: admin + MFA |
| **Base** | Document SHA-256 hashes | Server writes anchors |

Both backup buckets use **S3 Object Lock in Compliance mode (WORM)**: no user can delete or overwrite a locked object version until its retention expires, including account admins.

We use two buckets instead of one bucket with two folders because Object Lock's default retention applies to a whole bucket, and documents and dumps need different retention periods.

### What an attacker gets from each compromise

| Compromised | Attacker can | Outcome |
|---|---|---|
| Supabase DB | Edit or wipe tables, including snapshot rows | Snapshot edits detected (`SNAPSHOT_COMPROMISED`); DB restored from `lexchain-backup-db` |
| Supabase Storage | Tamper with or delete live PDFs | Detected (`TAMPERED`); restored from `lexchain-backup-docs` |
| EC2 server | Supabase key + limited AWS role | Same as above; backups can't be deleted (IAM deny + Object Lock) |
| A developer's AWS keys | EC2 actions only | Object Lock still blocks deleting backups |
| Lawyer's AWS admin login | — | Out of scope; Compliance mode still holds until retention expires |

---

## 3. AWS setup (console)

### 3.1 Account
- [ ] Enable MFA on the root user; don't use root day-to-day.
- [ ] Admin IAM user for the lawyer, with MFA.
- [ ] IAM users for developers with **EC2 permissions only**, with no S3 admin rights.

### 3.2 Bucket: `lexchain-backup-docs`
Bucket names are global, so add a unique suffix.
- [ ] Region `ap-southeast-1` (Singapore).
- [ ] Enable **Object Lock at creation**; this also turns on versioning.
- [ ] Block Public Access: on. Default encryption (SSE-S3): on.
- [ ] Default retention: **Compliance**.
  - Testing: **1 day**. ⚠️ Compliance retention cannot be shortened or removed.
  - Production: the firm's record-retention period.

### 3.3 Bucket: `lexchain-backup-db`
- [ ] Same settings as above.
- [ ] Default retention: **Compliance, ~30 days**.
- [ ] Lifecycle rule: expire objects after ~35 days, so dumps don't pile up forever. AWS deletes them once retention lapses, not the server.

### 3.4 EC2 IAM role (instance profile)
Attach this to the EC2 instances. No AWS access keys are stored on the server.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListBackups",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": [
        "arn:aws:s3:::lexchain-backup-docs",
        "arn:aws:s3:::lexchain-backup-db"
      ]
    },
    {
      "Sid": "WriteAndReadBackups",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": [
        "arn:aws:s3:::lexchain-backup-docs/*",
        "arn:aws:s3:::lexchain-backup-db/*"
      ]
    },
    {
      "Sid": "DenyDestructiveActions",
      "Effect": "Deny",
      "Action": [
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:DeleteBucket",
        "s3:PutBucketPolicy",
        "s3:DeleteBucketPolicy",
        "s3:PutBucketVersioning",
        "s3:PutBucketObjectLockConfiguration",
        "s3:PutLifecycleConfiguration",
        "s3:PutObjectRetention",
        "s3:PutObjectLegalHold",
        "s3:BypassGovernanceRetention"
      ],
      "Resource": [
        "arn:aws:s3:::lexchain-backup-docs",
        "arn:aws:s3:::lexchain-backup-docs/*",
        "arn:aws:s3:::lexchain-backup-db",
        "arn:aws:s3:::lexchain-backup-db/*"
      ]
    }
  ]
}
```

`s3:ListBucket` matters: without it, a missing object returns 403 instead of 404, and `exists()` can't tell "missing" from "forbidden".

**Optional hardening:** add a bucket policy that requires conditional writes (`If-None-Match: *`), so the server can't upload a new version over an existing backup key. Restore already re-hashes against the chain and rejects a wrong version, and the original version stays in the version history, so this is optional. Check the current AWS docs for the condition key.

---

## 4. File backup code

The `BackupProvider` protocol (`store` / `fetch` / `exists`, no delete) was designed for this swap, so callers don't change.

- [ ] `backend/app/infrastructure/backup/s3_backup.py`: `S3BackupProvider`
  - boto3 client using the default credential chain, which picks up the EC2 instance role.
  - Run blocking boto3 calls in `asyncio.to_thread`, like `SupabaseBackupProvider`.
  - `store`: `put_object(Bucket, Key, Body, ContentType)`. Object Lock buckets require an integrity checksum on upload; recent boto3 adds one automatically (otherwise pass `ChecksumAlgorithm="SHA256"`).
  - `fetch`: `get_object(...)["Body"].read()`.
  - `exists`: `head_object`; return `False` on 404.
- [ ] `factory.py`: add the `"s3"` branch.
- [ ] `core/config.py`: `BACKUP_PROVIDER=s3`, `BACKUP_BUCKET=lexchain-backup-docs-…`, `AWS_REGION=ap-southeast-1`.
- [ ] `requirements.txt`: `boto3`.
- [ ] Tests mirroring `tests/features/snapshot/test_backups.py` (stub the S3 client, or use `moto`).
- [ ] **Move existing archives:** switch the provider, then run `POST /admin/backups/verify` with repair. Existing `backup_key`s become "missing" in S3. Repair re-archives each file only if the snapshot matches the chain and the live copy matches the snapshot. No separate migration script.

**Scope note:** only **finalized (anchored)** documents are archived. Drafts and in-progress files are covered only by the database and Supabase Storage.

---

## 5. Database backup

- [ ] Celery beat task (e.g. `workers/backup_database.py`):
  1. Run `pg_dump` against Supabase.
  2. gzip the output.
  3. Upload it to `lexchain-backup-db/<UTC timestamp>.sql.gz`.
  4. Log `backup.db.stored` / `backup.db.failed`.
- [ ] Schedule: daily (`BACKUP_DB_SECONDS` in config, like `BACKUP_VERIFY_SECONDS`).
- [ ] Worker image: install `postgresql-client` with a version **≥ Supabase's Postgres version** (check with `select version();`), or `pg_dump` refuses to run.
- [ ] Use Supabase's **direct or session-mode** connection string; `pg_dump` won't work through the transaction pooler.
- [ ] Dumps contain personal data (RA 10173): they stay private and encrypted at rest (SSE-S3), and are readable only by the lawyer's admin account and the server role.
- [ ] **Restore drill:** restore one dump into a local Postgres and confirm the tables and row counts. Keep the steps in a short runbook.

---

## 6. Deployment

- [ ] EC2 instances (API + Celery workers) use the IAM role from 3.4.
- [ ] Server environment holds the Supabase keys and the chain recorder key.
- [ ] **Never on the server:** the AWS admin/root login.

---

## 7. Verification & defense demo

| # | Action | Expected |
|---|---|---|
| 1 | Finalize a document | Object appears in `lexchain-backup-docs` as `<document_id>/<sha256>.pdf` |
| 2 | Edit the live PDF in Supabase Storage → Verify | `TAMPERED` + tamper report (diff) |
| 3 | Restore | Archived original re-hashed against chain → becomes current → Verify: `AUTHENTIC` |
| 4 | Edit the snapshot row in the DB → Verify | `SNAPSHOT_COMPROMISED` |
| 5 | From EC2: `aws s3 rm s3://lexchain-backup-docs/<key>` | `AccessDenied` (IAM deny) |
| 6 | From the lawyer's admin account: delete a locked version | Blocked (Object Lock, Compliance) |
| 7 | Run the DB backup task | `.sql.gz` appears in `lexchain-backup-db` |
| 8 | Restore drill | Dump loads into local Postgres |

---

## 8. Paper updates

- [ ] Add the **threat model** paragraph (section 1).
- [ ] p. 9: the backup repository is stored with a separate provider (AWS) under Object Lock in Compliance mode (WORM). The Lawyer/Document Issuer may initiate backups and restore files but cannot alter or remove stored versions. The application server holds only a least-privilege credential (upload + read).
- [ ] p. 10: recovery no longer depends on "backup administration by the Lawyer"; it is enforced by Object Lock and IAM.
- [ ] p. 72, Functional Requirement 9: add WORM storage, least-privilege server access, and database backups.
- [ ] Table 31, Backup and Recovery row: separate-provider immutable backups for documents and the database; scheduled integrity verification against on-chain hashes; restore drill.
- [ ] **Limitations:** tampering by the firm itself, compromise of the owner's AWS admin credentials, and tampering before anchoring are out of scope. Only finalized documents are archived.
- [ ] **Supporting concepts to cite:** 3-2-1 backup rule; least privilege; ISO/IEC 27001:2022 A.8.13 (Information backup) and A.5.15 (Access control).

**Suggested wording (threat model):**

> The law firm, as system owner, is assumed trusted. LEXCHAIN defends against unauthorized modification by external attackers, including compromise of the application server, its credentials, and the database. Original documents and database dumps are backed up to a separate cloud provider under S3 Object Lock in compliance mode, a Write-Once-Read-Many (WORM) storage model in which no user, including account administrators, can modify or delete a stored version before its retention period expires. The application server holds only a least-privilege credential that can write and read backups but cannot delete or overwrite them. Integrity is verified against hashes anchored on the Base blockchain, and a tampered document is restored from the backup only after the backup is re-hashed and matched against the on-chain record.

---

## 9. Not covered by this plan

- **Contract ownership.** `correctDocumentHash()` lets the contract owner replace an anchored hash, and the owner key currently sits on the server. Under this threat model, a compromised server could overwrite an anchor, after which Verify would return `AUTHENTIC`. Handle it separately: restrict ownership through `setRecorder` / `transferOwnership`, or state it as a limitation.
