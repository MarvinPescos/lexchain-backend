# Risk clauses before finalize (#43)

Branch `fix/risk-before-finalize`. Closes #43.

## What changed for the lawyer

Before this change, a document's risk clauses were shown only **after** it was
finalized and anchored, when the signed copy could no longer be replaced. Now
the signed copy is processed as soon as it lands, while the document is
`SIGNED`:

```
signed copy attached (Path 1) / uploaded (Path 2)
  → SIGNED, status QUEUED → PROCESSING → AWAITING_REVIEW
  → lawyer approves the OCR text → ENRICHING → COMPLETED (risk_flags shown)
  → clause needs action?  replace the signed copy → processing starts again
  → otherwise            finalize (allowed only at COMPLETED) → freeze + anchor
```

Finalize queues no OCR. The on-chain hash is still the SHA-256 of the signed
PDF bytes. OCR text, review edits and insights never feed it.

## Backend

| Area | Change |
|---|---|
| `workflow/intake.py` | `DocumentIntake(session, queue)`. Attach, upload and replace record a `JOB_EXTRACT` dispatch and set `QUEUED` in the same commit as the signed copy, then send it. If the broker is down, the dispatch stays pending and `dispatch_pending` resends it. |
| `replace_signed_copy` | Locks the document row, then clears the old copy's extraction, chunks, insights and **unsent** dispatches, sets `QUEUED` and queues the new copy. The old copy stays in history. |
| `workers/process_documents.py` | Both jobs take `signed_copy_id`. A job for a copy that is no longer current does nothing. Every commit goes through `_commit_if_current`, which locks the document row and raises `SignedCopyReplaced` if the copy changed. A replace during OCR or enrichment therefore wins, and a stale job cannot mark the new copy `FAILED`. Enrich now runs only while the document is `ENRICHING`. |
| `dispatch.py` | `send` locks the dispatch row first and skips one already sent or dropped by a replace, so a late send cannot queue a duplicate job or fail the request. `drop_pending` clears unsent jobs on replace. |
| Queues, Celery tasks | Every job carries the current copy's id. Celery tasks default `signed_copy_id=None` (meaning the current copy), so messages already queued before the deploy still run. |
| `workflow/finalization.py` | `DocumentFinalization(session, provider, backup)`, without the queue. Locks the row and refuses unless `status == COMPLETED`: 409 `PROCESSING_NOT_COMPLETE`, with `details.status` and a message per status (still processing / review the text / failed, retry). The extract dispatch is gone. |
| `access.py` | `can_finalize` additionally needs `status == COMPLETED`. |
| `extraction/service.py` | Review and approve now happen while `SIGNED`. Before, they were allowed only at `FINALIZED`. `FINALIZED` stays allowed only for documents finalized under the old flow that are still mid-pipeline. Writes lock the document row, so a replace cannot clear the extraction mid-approve. |
| `nlp/analysis/llm_analysis.py` | The whole text is analyzed. A long document is split into ≤24k-char sections on paragraph breaks and the results are merged (labels, entities and risk flags deduplicated). `meta.analysis_status` is `complete`, or `partial` with `failed_sections`. Unparseable output is retried once per section. If no section can be read, the analysis raises `AnalysisFailed`, which is retryable, so enrichment retries and then the document is `FAILED`. A failed analysis no longer looks like "no risks". |
| `GET /documents/{id}` | New `analysis_status` field (`complete` / `partial` / null). |
| Migration `d2f8a61c9b47` | Data only. Documents already `SIGNED` with a null status get an extract dispatch and `QUEUED`, back-dated so the beat task sends them right away. Without it, they could never be finalized. |

Unchanged: `DocumentProcessing.retry` works the same at `SIGNED`, and restoration does not touch processing.

## Frontend (coordinate separately)

Full flow in `frontend-flows.md`. In short:

- After attach or upload, show the processing status on the `SIGNED` document and poll while it is `QUEUED`, `PROCESSING` or `ENRICHING`.
- OCR review (`AWAITING_REVIEW`) and the risk clauses move from the `FINALIZED` screen to the `SIGNED` screen.
- Keep **Finalize** disabled until `permissions.can_finalize` is true, which needs `status: COMPLETED`. Handle 409 `PROCESSING_NOT_COMPLETE`.
- After a replace, the detail comes back with `status: QUEUED` and empty `risk_flags` and `summary`. The new copy is processed from the start.
- `analysis_status: "partial"`: warn that some sections could not be analyzed.
- After finalize, `status` stays `COMPLETED`. It no longer goes back to `QUEUED`.

## Deploy notes

- Run `alembic upgrade head` (migration `d2f8a61c9b47`).
- The untracked email-invites migration `c4e8a2d61f37` on `feat/document-lifecycle` also revises `b3d7e9f15a26`. Whichever branch merges second has to point its `down_revision` at the other, or alembic ends up with two heads.

## Tests

`tests/features/documents/test_risk_before_finalize.py` covers:

- risks shown at `SIGNED` on both paths
- broker down at attach
- the finalize gate for each non-`COMPLETED` status
- finalize anchoring the current copy and queuing nothing
- replace: clears the old data, processes the new file only, drops unsent jobs
- a late job for a replaced copy
- a replace during OCR and during enrichment
- a stale failure not marking the new copy `FAILED`
- a long document analyzed to the end
- unparseable output (retried, then raised; partial)

`tests/infrastructure/test_migrations.py` covers the backfill. Existing tests now process before they finalize, using the `mark_processed` helper in `conftest.py`.

## Out of scope (per #43)

- Risk review of the Google Docs draft during `PREPARING`
- Withdrawing a document from `SIGNED`
- Re-processing already finalized documents (their anchors do not change)
