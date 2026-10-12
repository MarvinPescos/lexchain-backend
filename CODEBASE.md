# LexChain Backend — How It Works

Context doc for anyone (human or agent) picking up this codebase. Auth, roles
and CRUD are summarised briefly; the **PROCESS** — the OCR → review → NLP →
anchor pipeline — is covered in depth, because that is where the design
decisions live.

---

## 1. Shape of the repo

```
backend/app/
  main.py                  FastAPI app, exception handlers, Langfuse flush middleware
  celery_app.py            Celery config + queue routing
  celery_tasks/            Thin task wrappers (documents, notifications)
  workers/                 The actual async pipeline jobs
  core/config.py           Pydantic Settings — every provider toggle lives here
  features/<name>/         Vertical slice: router → service → repository → models/schemas
  infrastructure/          Swappable adapters behind Protocols
    nlp/ocr | chunking | embeddings | llm | analysis
    blockchain | backup | email | queue | database | clients | security | observability
```

Two rules hold everywhere:

- **Feature = vertical slice.** `router.py` (HTTP + OpenAPI), `service.py` (rules),
  `repository.py` (SQL), `models.py` (SQLAlchemy), `schemas.py` (Pydantic),
  `dependencies.py` (DI).
- **Infrastructure = Protocol + factory.** Every external capability is a
  `typing.Protocol` with a `build_x()` factory that reads `settings`. Call sites
  never name a concrete provider, so mock/real swaps are a `.env` change
  (`USE_REAL_OCR`, `USE_REAL_LLM`, `USE_REAL_EMBEDDER`, `OCR_ENGINE`,
  `CHUNKER_ENGINE`, `BACKUP_PROVIDER`).

---

## 2. Low priority (short version)

- **Auth** — Supabase-issued JWT, verified in `infrastructure/security/`.
  `require_auth` yields an `AuthenticatedUser`; `require_lawyer` gates the
  write side of the pipeline (edit / approve / finalize / restore).
- **Access control** — centralised in `DocumentService._get_for_read` /
  `_get_for_write`. Other services do **not** re-implement it: `ExtractionReviewService._load`
  and `BlockchainService.verify_document` construct a bare `DocumentService`
  (`__new__` + attach session/repo) purely to reuse those two methods, so review
  and verify permissions cannot drift from the document endpoints.
- **Parties / invitations** — a document has parties with roles
  (`shared/constants.PARTY_ROLES`, `PRIVILEGED_PARTY_ROLES`) and a
  pending/accepted/declined status. Parties get read access; only the owner (or
  an admin acting for them) writes.
- **Books** — folders that documents belong to. **Notifications** — Celery
  fan-out (`notifications` queue) + Resend email. **Admin** — user/document
  oversight.
- **Errors** — everything raises `BaseAppException` subclasses
  (`BadRequestError`, `ForbiddenError`, `NotFoundError`, `ConflictError`), and
  `main.py` normalises framework errors (HTTPBearer 401, 404/405, Pydantic 422)
  into the same `{error_type, message, status_code}` shape.

---

## 3. THE PROCESS — end to end

### 3.1 The state machine

```
status:     UPLOADED → QUEUED → PROCESSING → AWAITING_REVIEW → ENRICHING → COMPLETED
                                                 ↑ human gate          ↘ FAILED
lifecycle:  DRAFT ────────────────────── finalize ──→ FINALIZED
              ↑ new version supersedes                      ↓ superseded
              └───────────────────────────────────── ARCHIVED
```

`status` = how far processing got. `lifecycle` = whether the version may still
be mutated (`IMMUTABLE_LIFECYCLES = (FINALIZED, ARCHIVED)`). Both in
`features/documents/constants.py`.

### 3.2 The central design decision: the pipeline is **split in two**

The whole architecture turns on one call: **nothing is derived from OCR text
until a human approves it.**

| Phase | Celery task | Queue | Worker fn | Ends at |
|---|---|---|---|---|
| 1 — Extract | `document.extract` | `documents` | `extract_document_job` | `AWAITING_REVIEW` |
| **gate** | — | — | issuer edits + approves over HTTP | — |
| 2 — Enrich | `document.enrich` | `documents` | `enrich_document_job` | `COMPLETED` |

Why (from `workers/process_documents.py`): chunking, embedding and analysis
deliberately do *not* run in phase 1, because building them from unreviewed text
would mean the RAG index and the on-chain snapshot disagree the moment the issuer
corrects anything. Phase 2 runs on `extraction.approved_text` — the same string
that finalize snapshots and hashes.

Task routing is wildcarded (`"document.*": {"queue": "documents"}`) so renaming
or adding a document task can't silently strand it in a queue no worker consumes.
Both tasks are `acks_late=True`, `max_retries=3`, `default_retry_delay=60`.

### 3.3 Upload (`DocumentService.upload_document`)

1. Read bytes → `_validate_file` (non-empty, `application/pdf` only, ≤ 60 MB).
2. `document_hash = sha256(file bytes)` — the *file* hash, for dedup/integrity.
   This is **not** what gets anchored.
3. Upload to Supabase Storage; on any DB failure the object is deleted again.
4. Insert row: `status=UPLOADED`, `version=1`, `is_latest=True`,
   `lifecycle=DRAFT`, then `root_document_id = self.id` (v1 is the root of its
   own chain — only knowable after the flush).
5. Commit **before** enqueueing, then `enqueue_process_document` → `status=QUEUED`.
   If enqueue fails the response says so and leaves the doc at `UPLOADED` rather
   than lying about acceptance.

`update_document` appends a version instead of overwriting: new row with
`version+1`, `previous_document_id`, shared `root_document_id`, old row
`is_latest=False`. A FINALIZED version is never overwritten — superseding it just
appends a new DRAFT to the chain.

### 3.4 Phase 1 — OCR extraction

`extract_document_job(document_id, storage_url)`:

1. Download the file from storage over httpx.
2. `status=PROCESSING`.
3. `build_ocr(file_data)` — see below.
4. `ocr.extract()` → `ExtractionResult`.
5. Blocks: `ocr_result.blocks or blocks_from_text(ocr_result.ingestion_text)`.
6. Flags: `build_flags(blocks, ocr_result.ingestion_text)`.
7. Persist a `DocumentExtraction` row, `status=AWAITING_REVIEW`, notify the user.

`content_hash` is deliberately **not** set here — it must describe the text the
issuer approved.

Everything is wrapped in a Langfuse span (`document_extraction`) with nested
spans for `ocr_extraction`, and `langfuse.flush()` is called *outside* the
`with` so the span is closed before it ships. Failures roll back, mark `FAILED`
in a **fresh session** (`_mark_failed` opens its own `AsyncSessionLocal` because
the original one is poisoned), and notify.

#### Engine selection — `infrastructure/nlp/ocr/factory.py`

```
USE_REAL_OCR=false          → MockOCRProvider (canned contract text)
OCR_ENGINE=mineru           → MinerUOCRProvider           ← the real path
OCR_ENGINE=pypdf + digital  → PyPdfOCRProvider
OCR_ENGINE=pypdf + scanned  → TesseractOCRProvider (auto-fallback)
```

`_is_scanned_pdf` is a pre-flight: pull text from the first 3 pages with pypdf;
under 50 characters means the pages are images, so pypdf would return an empty
document — fall back to rasterise-and-OCR rather than succeed with nothing.

#### `ExtractionResult` — and why there are two texts

```python
class ExtractionResult(BaseModel):
    text: str             # render text — HTML tables with inline styles, for the UI
    ingestion_text: str   # tables converted to markdown, for chunking/embedding/LLM
    page_count: int
    blocks: list[dict]    # positioned blocks; empty for engines with no layout
    meta: dict | None     # engine, engine_version, mode, confidence_avg
```

`MinerU._process_tables` builds both from the same soup: the render side styles
`<table>/<th>/<td>` so the review screen shows a table instead of markup; the
ingestion side runs `markdownify` over each top-level table so the downstream
chunker and LLM see linearised rows instead of tag soup. Inline images are
stripped (`![...](...)`) — MinerU emits links to files we don't keep, and they
would otherwise become orphan noise in the chunks.

#### Blocks — the unit of truth once review starts

MinerU returns a `content_list`; `_parse_blocks` normalises each entry to:

```python
{ "index", "editable", "type", "text", "original_text",
  "bbox", "page_idx", "text_level", "score", "is_html", "edited" }
```

Non-obvious calls baked into that function:

- **Tables carry their content in `table_body` as HTML, not `text`.** Reading
  only `text` would silently drop them from review *and* from the approved text
  that gets anchored — on a notarial document the table is often the operative
  content.
- **Figures, seals and signatures are kept but `editable=False`.** They have no
  text, but the review UI outlines every region, and a notarial seal missing
  from the overlay reads as a gap. They never reach the approved text
  (`blocks_to_text` filters on `editable`).
- **`bbox` is normalised to a 0–1000 box per page, each axis scaled
  independently** — aspect ratio is *not* preserved, so a client maps
  `x/1000*renderedWidth` and `y/1000*renderedHeight` separately. This is
  documented on the schema field because getting it wrong silently misplaces
  every highlight.
- Engines with no layout (pypdf, plain tesseract) produce no blocks, so
  `blocks_from_text` splits on `\n\n` into paragraph blocks with `bbox=None`.
  The review flow is identical either way; only page highlighting is lost.

### 3.5 Flagging — the triage layer (`extraction/flagging.py`)

The goal is stated in the module docstring: **triage, not correctness.** A
20-page agreement is unreadable in practice, so every flag carries the block it
belongs to (and therefore a page and a bbox), turning review into "check these 6
spots" instead of "read everything".

Four tiers. Tiers 1–3 are deterministic, free, and run at extraction time:

**Tier 1 — engine confidence.** Blocks with `score < 0.85` are surfaced;
`< 0.6` is `high`, else `medium`.

**Tier 2 — garbled text.** Regex families for classic OCR corruption:

| Pattern | Catches | Severity |
|---|---|---|
| `_GLUE_DIGIT_ALPHA` / `_GLUE_ALPHA_DIGIT` / `_GLUE_CASE` | `20thday`, `June2026`, `theContractor` | medium |
| `_MIXED_ALNUM` | `0`/`O`, `1`/`l` substitutions inside a word | low |
| `_PUNCT_NOISE` | runs of 3+ symbols — scan noise | low |

The carve-outs matter more than the patterns: `{3,}` on the glue rule keeps bare
ordinals (`20th`) from matching; `_ORDINAL` and a title-number shape
(`[A-Z]?-?\d+[A-Z]?`) are excluded from the mixed-alnum tier; anything already
reported as a glue error is not reported again. Precision over recall — a flag
list full of false positives gets ignored wholesale.

**Tier 3 — domain consistency (Philippine legal documents).**

- **Amount in words vs figure.** `_words_to_number` parses
  `"FIVE HUNDRED THOUSAND"` → `500000` (units + `hundred/thousand/million`
  scales, ignoring `and/only/pesos`) and compares it to the `PHP/₱/P` figure that
  follows it in the same block. Mismatch = `high`. Returns `None` when nothing
  recognisable is present, so a bogus mismatch is never reported.
- **Dates** — `"20th day of June, 2026"` must name a real month and a day in
  1–31.
- **Title numbers** — a `TCT No. …` with no digits in it came through as noise.
- **Series year vs execution year** — a notarised instrument is entered in the
  register for the year it was executed, so `Series of 2026` disagreeing with the
  execution year is a filing error worth catching *before* anchoring. Document-level,
  so `block_index=None`.
- **Notarial block completeness** — `Doc. No / Page No / Book No / Series of`.
  Only reported if *some* are present (`missing < all`), so a non-notarial
  document doesn't get flagged for lacking a notarial block it never had.

Table blocks are run through `_block_text` first, which strips tags and unescapes
entities — otherwise the garbling heuristics report `</td><td>` as punctuation
noise and `&#x27;` as a letter/digit mix.

Output is sorted `high → medium → low`, then by block index.

**Tier 4 — LLM semantic review (`extraction/llm_review.py`), opt-in.**
`POST /documents/{id}/extraction/analyze`.

The division of labour is explicit: the deterministic tiers own everything
*countable* — amounts, dates, title numbers, confidence, glue errors — because
an LLM asked to check arithmetic produces confident wrong answers. Tier 4 is for
judgement only: a party's name drifting between clauses, contradictory
obligations, a term used but never defined, a referenced annex that is absent, a
sentence too garbled to read. The system prompt explicitly forbids reporting
spelling, spacing, formatting or arithmetic, and ends with "Never invent problems
to seem useful."

Hardening on the way back in:

- blocks rendered as `[index] text`, truncated at `MAX_CHARS = 24_000`;
- `temperature=0.0`, `max_tokens=1024`;
- `_parse_findings` tolerates ```json fences and leading prose;
- a `block_index` not in the real block set is **dropped to `None`**, not
  surfaced — pointing the issuer at a block that doesn't exist is worse than no
  location;
- severity clamped to `{medium, low}` — the LLM cannot raise a `high`;
- any exception returns `[]`: a failed second opinion must never block a review.

Re-running replaces the previous LLM pass (`kind != "llm_inconsistency"` are
kept) so flags don't accumulate.

### 3.6 The human gate — `ExtractionReviewService`

`DocumentExtraction` stores raw and reviewed side by side:

| Column | Meaning |
|---|---|
| `text` | what the engine produced — **never mutated**, the evidence of what the machine read |
| `blocks` | JSONB; current text + `original_text` + bbox per block |
| `flags` | JSONB; recomputed on every edit |
| `reviewed_text` | the frozen approved string |
| `reviewed_by` / `reviewed_at` | who attested and when |
| `edited_block_count` | how much the issuer changed |
| `approved_text` (property) | `reviewed_text if not None else text` |

**`GET /documents/{id}/extraction`** — blocks, flags, counts, review state.

**`PATCH /documents/{id}/extraction`** — repeatable, does not approve:

- `copy.deepcopy(extraction.blocks)` **before** touching anything. SQLAlchemy
  does not track in-place mutation of JSON/JSONB, so a shallow copy would share
  the loaded dicts, the "new" value would compare equal to the old, and the
  column would never be written.
- Rejects an unknown index, and rejects editing a non-`editable` (figure/seal)
  block.
- `block["edited"] = edit.text != block["original_text"]` — compared against the
  OCR original, so *reverting* an edit clears the marker rather than leaving it
  stuck on.
- Re-runs `build_flags` over the corrected text so fixed problems stop being
  reported.
- `flag_modified(extraction, "blocks"/"flags")` as belt-and-braces so a silently
  skipped UPDATE can't turn a review into a no-op.

**`POST /documents/{id}/extraction/approve`** — the gate:

1. `approved_text = blocks_to_text(blocks)` — editable, non-empty blocks joined
   with `\n\n` in index order.
2. `content_hash = sha256(approved_text)` written to the **document**.
3. `reviewed_text/by/at` written to the extraction.
4. `status = ENRICHING`, then `enqueue_enrich_document`.

Write guards (`_load(for_write=True)`): already approved → 409; lifecycle not
DRAFT → 409; status not `AWAITING_REVIEW` → 409. Approval is one-way.

### 3.7 Phase 2 — enrichment

`enrich_document_job(document_id, analyzer, chunker, embedder)` — providers are
built in the Celery task, not the worker, so the job is injectable in tests.
`status=ENRICHING`, then three stages inside a `document_enrichment` span, all on
`extraction.approved_text`.

#### 1. Chunk

`build_chunker()`: `CHUNKER_ENGINE=markdown` → `MarkdownChunker`, otherwise
`SpacySentenceChunker` (with a warning log, since it's the fallback).

- **`MarkdownChunker`** (LangChain) — pairs with MinerU, whose output is
  markdown. `MarkdownHeaderTextSplitter` on `#/##/###` with `strip_headers=False`
  so each chunk keeps its heading (the heading is retrieval signal), then
  `RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)` inside each
  section. **A section containing a table (`^|`) is emitted whole and never
  character-split** — half a table is worthless in a retrieved context. Section
  metadata (h1/h2/h3) rides along on each chunk.
- **`SpacySentenceChunker`** — `en_core_web_sm`, loaded once at module level
  (segmentation only, no vectors). Packs sentences to `max_words=800` with a
  1-sentence overlap. Used when the source isn't markdown; the trained segmenter
  handles abbreviations and decimals that a regex splits wrongly.
- **`SentenceChunker`** — regex placeholder, `max_words=7`, not wired into the
  factory.

Rows are `bulk_create`d into `document_chunks` with `embedding=None`, carrying
`document_id`, `extraction_id`, `chunk_index`, `strategy`, `engine`,
`engine_version`, `meta`. `UniqueConstraint(extraction_id, chunk_index)` means
chunks belong to a *specific extraction*, so a re-extraction can't collide with
the old set.

#### 2. Embed

`build_embedder()`: `USE_REAL_EMBEDDER` → `SBERTEmbedder(SBERT_MODEL)`
(default `all-MiniLM-L6-v2`, 384-dim), else `MockEmbedder(dim=384)` — same
dimension so the column and the queries stay valid either way.

`SentenceTransformer` is cached on the class (`_model` / `_model_name`) so the
model loads once per worker process, and `encode` runs in `asyncio.to_thread`
because the library is sync and CPU-bound. `normalize_embeddings=True` — with
unit vectors, cosine distance is the only similarity that needs computing.

`embed_batch` over every chunk text, then `set_embedding` writes **both**
`embedding` (JSONB: `{vector, meta}`) and `embedding_vec` (`pgvector.Vector(384)`).
The JSONB column is the historical/debug copy; `embedding_vec` is what search
uses. Empty chunk list short-circuits with `{"skipped": True}` on the span.

#### 3. Analyze

`LLMAnalysisProvider.analyze(text)` — one call, whole document:

- Truncated to `max_chars = 24_000` (~6k tokens at ~4 chars/token) with a
  `analysis.text_truncated` warning, leaving room for the system prompt and output.
- `temperature=0.1`, `max_tokens=1024`.
- The system prompt pins an exact JSON shape — `summary` (2–3 sentences naming
  document type, number, issuing org, date, purpose), `labels` (document-type +
  topic), `entities` (`PERSON, ORG, DATE, MONETARY, LOCATION, URL,
  DOCUMENT_NUMBER, DOCUMENT_REFERENCE`), `risk_flags`
  (`{clause, severity}` with low/medium/high defined against real consequences:
  routine → deadline/compliance → penalty/sanction/financial exposure). It is
  told twice to use only the document text and to return `[]` rather than invent.
- `_parse` catches `JSONDecodeError` and degrades to a zero-confidence result
  with `error: json_parse_failed` — a bad LLM response never fails the job.
- `_compute_confidence` is a **signal-based heuristic, not a model
  probability**: start at 0.1, subtract for a missing summary (−0.3), no
  entities (−0.2), truncated input (−0.1), or a near-empty document (−0.2),
  floored at 0. Treat it as a "how much did the extractor actually find" score.

Result lands in `document_insight` (one row per document, linked to the
extraction that produced it), with lists wrapped as `{"items": [...]}`.

Then `status=COMPLETED` and a notification.

### 3.8 LLM access — one door

`build_llm(model=None)` is the only constructor. `USE_REAL_LLM=false` → mock;
otherwise `OpenRouterLLMProvider`, which reuses the OpenAI `AsyncOpenAI` client
against `OPENROUTER_BASE_URL` because OpenRouter speaks the chat-completions API.
Switching models is `OPENROUTER_MODEL` (default
`meta-llama/llama-3.3-70b-instruct`), never a new class. Returned `meta` carries
`finish_reason`, token counts, and OpenRouter's `provider` field — which upstream
actually served the request, the thing you need when debugging quality or latency.

Three callers, three jobs: **analysis** (structured extraction), **llm_review**
(semantic flags), **RAG** (question answering).

### 3.9 Retrieval / RAG

`RetrievalService`:

- `search_document(user_id, document_id, query, top_k=5)` — embeds the query,
  then `chunk_repo.search_document`: `embedding_vec.cosine_distance(q)`, ascending,
  limit `top_k`, skipping NULL vectors. Score returned to the caller is
  `1.0 - distance`. Access falls back to `is_party` when the user isn't the owner.
- `search_all(user_id, …)` — same, joined to `documents` filtered by owner.
- `search_public(…)` — adds a `threshold` (default 0.75, applied as
  `distance <= 1 - threshold`).

`RAGService.ask_document` (`@observe(name="RAG_Question_Answer")`): retrieve
`top_k=2` → build context as `[chunk {index}]\n{text}` → system prompt pinning
the model to the context ("if the answer is completely missing, say 'I cannot
find this in your documents'") while still allowing normal conversation →
`temperature=0.2`, `max_tokens=512` → return `{question, answer, model, citations}`
where each citation is `{chunk_id, chunk_index, score}` (+ `document_id` on the
global variant). `ask_all` exists but is commented out.

### 3.10 Finalize — the only on-chain write

`DocumentService.finalize_document` preconditions, in order: not already
finalized → `is_latest` → lifecycle is DRAFT → `status == COMPLETED` → an
extraction exists → **`extraction.reviewed_at is not None`**. The last one is the
point of the whole gate.

Then:

1. `create_snapshot(document_id, extraction.approved_text, blocks=extraction.blocks)`
   → `document_snapshots` (unique per document, `ondelete=RESTRICT`, never
   updated). `text_hash = sha256(approved_text)`. Blocks are frozen alongside but
   **only `text` is hashed** — the blocks are evidence of *where* the text sat, so
   a later tamper diff can point at a region of the page.
2. `blockchain_service.record_document(document, snapshot.text_hash)` — provider
   (`web3` or mock) writes the hash, an `on_chain_records` row stores
   `tx_hash / onchain_document_id / data_hash / onchain_timestamp / issued_by`,
   and `document.on_chain = True`.
3. `finalized_by`, `finalized_at`, `lifecycle = FINALIZED`. Commit.
4. `_archive_document` — **after** the anchor, never before: the document is
   provably attested at that point, and a failed upload must not undo it.
   Copies the PDF plus a self-describing `manifest.json` (hashes, version,
   timestamps) to the backup bucket, then sets `backed_up_at` / `backup_key`.
   Failures are logged, not raised; the audit query for gaps is
   *finalized AND `backed_up_at IS NULL`*.

**Three hashes, do not confuse them:**

| Hash | Over | Written | Anchored? |
|---|---|---|---|
| `document.document_hash` | raw PDF bytes | upload | no |
| `document.content_hash` | approved text | approve | no |
| `snapshot.text_hash` | approved text | finalize | **yes** |

### 3.11 Verify — and why it re-runs OCR

`BlockchainService.verify_document` runs three steps in a deliberate order:

1. **Detect** — `_extract_current`: fetch the file with `fetch_uncached` (never
   through the CDN — a cached copy would be compared against the chain and
   produce a confident wrong verdict), re-run OCR, rebuild
   `blocks_to_text(blocks)`, hash it. Cached text is never used: it lives in the
   same database an attacker would have altered, so it would happily agree with
   the tampered copy. MinerU is deterministic, so an untouched file re-extracts
   identically.
2. **Prove the baseline** — hash the stored snapshot and compare it to the chain
   *before* diffing. An attacker with DB access could edit the snapshot too, and
   diffing against a doctored baseline gives a confident, wrong answer.
3. **Localize** — diff attested vs current.

Verdicts:

| status | meaning |
|---|---|
| `NOT_ANCHORED` | still a draft; nothing to verify against |
| `AUTHENTIC` | current hash == on-chain hash |
| `TAMPERED` | mismatch, baseline proven genuine → full tamper report |
| `SNAPSHOT_COMPROMISED` | both current *and* snapshot disagree with the chain → refuses to diff |
| `VERIFICATION_UNAVAILABLE` | OCR or storage unreachable — integrity **unknown**, explicitly not a tamper result |

That last one is a 200 with an honest message rather than a 500, and never
implies the document is either sound or tampered.

### 3.12 Tamper localization (`blockchain/tamper.py`)

"A hash is a smoke alarm, not a security camera." `difflib` only — no new
dependency.

`diff_blocks` (preferred) compares editable blocks and carries `page_idx` +
`bbox`, so a client outlines the altered region on the page. `bbox` comes from
the **current** document — where the altered text physically sits now — with the
attested position alongside. Snapshots taken before block layout was stored fall
back to `diff_text`, which is line-level.

`_classify` ranks each change by what it would do to the agreement, checked in
this order:

| Check | Severity |
|---|---|
| monetary amount changed / removed / inserted | `critical` |
| reference number changed (TCT, CCT, PTR, IBP, MCLE, Roll, Doc/Page/Book, "Transfer Certificate of Title No.") | `critical` |
| date changed | `major` |
| other figures changed | `major` |
| wording only | `minor` |

Order is deliberate: identifiers are checked before the generic number rule so a
swapped title number reads as "Reference number changed" rather than being
demoted to "Figures changed" — a different title number points the instrument at
a different property.

A `replace` opcode covering equally-sized runs is **split 1:1 per line/block**,
because otherwise a changed amount adjacent to a changed title number collapses
into a single segment and the second change disappears. Each segment also gets a
word-level `_word_diff` (`equal/added/removed`) for inline highlighting. Output:
segments sorted critical→minor, plus `total_changes`, `critical_changes`,
per-op counts, and a `similarity` ratio.

### 3.13 Restore

`POST /documents/{id}/restore` — manual, run after `verify` reports tampering:

1. Finalized + `backup_key` required.
2. Fetch the archived copy, **re-OCR it and check its hash against the chain**.
   A backup that doesn't match is refused, not restored — never trust the archive
   blindly.
3. Preserve the current (tampered) file to
   `documents/{id}/v{n}/tampered/{timestamp}.pdf` first — on a notarised document
   the tampered copy is potentially evidence. A failure here downgrades to a
   warning, since a missing current file is itself the damage being repaired.
4. Overwrite the exact object the document's `storage_url` points at (via
   `object_path_from_url`, not a path rebuilt from `file_name`, which would write
   a new object and leave the tampered one live).

---

## 4. Observability

Langfuse wraps the pipeline: a root span per job (`document_extraction`,
`document_enrichment`), nested spans for `ocr_extraction`, `chunking`,
`embedding`, and a `generation` for `analysis_llm`. `propagate_attributes`
attaches `user_id` and filename. `langfuse.flush()` is always called after the
span closes — inside `main.py` as middleware for requests, after the `with` block
in workers. Structured logs go through `structlog` (`log.info("worker.document.completed", …)`)
with dotted event names.

---

## 5. Known rough edges

- `ChunkResult.strategy` vs a legacy `stategy` typo — the worker reads both via
  `getattr` fallback.
- `_run_embeddings` is annotated `-> None` but returns the embedding dim.
- `RAGService.ask_all` / the global `/ask` route are commented out.
- `DocumentChunk.embedding` (JSONB) duplicates `embedding_vec` (pgvector); only
  the latter is queried.
- `BACKUP_PROVIDER=supabase` is interim — a same-project bucket survives
  accidental deletion but not a compromised account. The config comment says to
  replace it with an object-locked bucket in a separate account.
- `_extract_current` assumes MinerU-style determinism; a tesseract fallback path
  would not reliably re-hash identically.
