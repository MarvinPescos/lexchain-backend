# Tamper coverage

What LexChain detects and recovers, what it doesn't, and how each gap is handled.

**The claim we can defend:** finalized documents are tamper-evident and recoverable, and the database is recoverable from immutable backups.

Not "the whole system is tamper-proof". The chain only stores a document ID, the PDF hash, a timestamp and the issuer, so anything else needs its own protection.

Every attack, its status and its test is listed in `issues/03-tampering-attacks-and-database-recovery.md`.

## Three separate jobs

| Job | Question | Provided by |
|---|---|---|
| Proof | What is genuine? | On-chain hash |
| Detection | Did something change? | Verify, scheduled checks |
| Recovery | Can we get it back? | S3 archive (PDFs), RDS point-in-time recovery and DB dumps (database) |

Recovery depends on detection. RDS recovery reaches back up to 35 days and hourly dumps 7 days, so a change noticed later than that needs a monthly dump, which only has the value as of that month.

## How each kind of tampering is handled

| Tampered | Detected by | Recovered from | Status |
|---|---|---|---|
| Live PDF edited | Verify on open, scheduled live check | Archive | Validated on the S3 archive |
| Live PDF deleted | Verify and scheduled live check (`FILE_MISSING`) | Archive | Validated |
| Archive | Scheduled check, which notifies the lawyer and `BACKUP_ALERT_EMAIL`; blocked by Object Lock on S3 | Not needed | Blocking validated on S3. Alerts built (#55), rerun pending |
| Snapshot row | Verify and the scheduled check, which alerts | Restore, from the chain and archive. `signed_copy_id` needs a DB backup if no signed copy matches the chain | Built (#55), rerun pending |
| `lifecycle` changed to hide tampering | Verify reads the chain first; the scheduled check alerts | Restore | Built (#55), rerun pending |
| Anchor overwritten with the recorder key (C1) | Blocked by the contract | Not needed | Validated on the current contract. The contract the app still points at allowed it |
| Anchor replaced with the owner key (C2) | Scheduled watch of the key's events; `anchor_corrected` on verify | Manual, using the previous hash in the alert | Detected, not fixed: the key can still do it. Built (#55), rerun pending |
| Database wiped | Obvious from the outage | RDS (about 5 minutes lost) or dumps (up to 1 hour lost) | Validated. A dump doesn't restore `auth.identities`; email sign-in still works |
| Single row deleted | Reconcile job | DB backup | Detection validated; restore validated through RDS |
| Metadata | Verify and the scheduled check, against the `{document_id}:metadata` anchor | Restore, from the archived JSON | Built (#55), rerun pending. Documents need the re-anchor script to get an anchor |
| OCR text, chunks, insights | Not detected | Regenerate from the verified PDF; review edits need a DB backup | Accepted limitation |
| Audit and verification history | Not detected | DB backup, if noticed in time | Accepted limitation |

## Whole-record recovery

When a document's database records are damaged, not just its file:

1. Restore the row from the newest DB backup before the change: RDS to just before it, or a dump.
2. Verify the PDF against the chain, and restore it from the archive if it doesn't match.
3. Regenerate OCR text, chunks and insights from the verified PDF. Take review edits from the backup, because a person made those.

## Work needed

| Order | Work | Tracked in |
|---|---|---|
| 1 | Point the app at the current contract and re-anchor | `issues/03-tampering-attacks-and-database-recovery.md` |
| 2 | Stop denied requests tripping the missing-dump alarm | `issues/03-tampering-attacks-and-database-recovery.md` |
| 3 | Restore `auth.identities` from a dump | `issues/03-tampering-attacks-and-database-recovery.md` |
| Deferred | Owner key off the server (C2) | `backup&recovery_implementation.md`, contract ownership step |

## Accepted limitations

Options if any of these need closing later:

- **Chunks and insights:** store a hash at generation and check it before use. RAG answers and risk analysis read this data, so an edited chunk can change an answer while the document still shows `AUTHENTIC`. The stored hash sits in the same database, so a careful attacker could update both.
- **Audit history:** periodically anchor a hash of the log. Skipped for now because of the added complexity.

Already out of scope in the backup plan:

- Tampering by the firm itself
- Compromise of the owner's AWS admin credentials or owner wallet
- Tampering before anchoring. A review prompt before finalize would reduce wrong files being anchored, since nothing else can tell whether the upload was the correct document
- Verdicts while the server itself is compromised, since verify runs there
