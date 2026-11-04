# LexChain — Path 1 and Path 2 flows (for the front-end agent)

How a document moves through LexChain, screen by screen, with the API call
behind each step. Read this for the **flow**; read `frontend-handoff.md` for
connection setup, full response shapes and the Google Picker details. The live
schema is the source of truth for fields: `GET {BASE_URL}/openapi.json`.

Checked against the backend on branch `feat/document-lifecycle`, 2026-10-16.

## Ground rules

- **LexChain does not notarize.** Signing and notarization happen on paper, in
  person. LexChain prepares the text (Path 1), files the signed PDF, and on
  *Finalize* records the PDF's sha256 on the blockchain (Base Sepolia).
- **One document, two ways in.** Both paths end in the same record and the same
  OCR → risk check → finalize → verify steps. Only the start differs.
- **Never compute permissions in the UI.** Every document response carries
  `permissions` (`can_mark_ready`, `can_finalize`, …). Show a button only when
  its flag is `true`. The stage rules below explain *why* a flag is on or off;
  they are not for the UI to re-implement.
- Every action endpoint returns the updated document detail — replace local
  state with it, don't patch it by hand.
- Errors: `{error_type, message, status_code, details}`. Show `message`; branch
  on `details.reason` when present.
- Lawyers (`role: lawyer`) do everything. Clients (`role: user`) only view
  documents they accepted an invitation to and comment in Google Docs — all
  their action flags are `false`.

## Stages (`lifecycle`)

```
Path 1:  PREPARING ⇄ READY_FOR_SIGNATURE ─┐
                                          ├→ SIGNED → FINALIZED
Path 2:  (upload) ────────────────────────┘

CANCELLED: only from PREPARING or READY_FOR_SIGNATURE (never after signing)
```

| Stage | Meaning | Lawyer can |
|---|---|---|
| `PREPARING` | Text being drafted in Google Docs with the client | create draft (once), invite clients, mark ready, rename, cancel |
| `READY_FOR_SIGNATURE` | Text agreed and frozen; Doc is read-only; goes to print | attach signed copy, back to preparing, rename, cancel |
| `SIGNED` | Signed PDF is filed with a register entry and processed: OCR review, risk clauses | OCR review, replace signed copy (with reason), correct Doc./Page No., rename, search, ask, finalize (once `COMPLETED`) |
| `FINALIZED` | PDF hash anchored on chain; frozen forever | verify, search, ask |
| `CANCELLED` | Abandoned before signing; kept for the record | nothing |

`status` is a separate field — the OCR/NLP pipeline for the current signed
copy. It is `null` until a signed copy is attached or uploaded, then `QUEUED →
PROCESSING → AWAITING_REVIEW → ENRICHING → COMPLETED` (or `FAILED`), all while
`SIGNED`. Replacing the signed copy sets it back to `QUEUED`. Show it on
`SIGNED` and `FINALIZED` documents.

---

## Path 1 — prepared with LexChain

Use when the lawyer drafts the document with the client before it is signed.

### Before the first draft (once per lawyer)

The lawyer's Google account must be connected — drafts live in **their own**
Google Drive.

1. `GET /google/connection` → `{connected, google_email}`.
2. Not connected → tell the lawyer *"On Google's screen, tick the Google Drive
   permission"*, then `POST /google/connect` → `{authorization_url}` and
   **navigate the whole window** there.
3. Google returns to the front-end settings page with `?google=connected` or
   `?google=error&message=…`. Show the result.

Any draft call can return 409 `GOOGLE_NOT_CONNECTED` or
`GOOGLE_RECONNECT_REQUIRED` → send the lawyer back to step 2.

### Step 1 — open the record

Lawyer clicks **New document**.

`POST /documents/` `{file_name}` → 201, `lifecycle: PREPARING`. No file, no
Doc. No. yet — the register number only exists once it is signed.

### Step 2 — invite the client

`GET /users/search` to find the client (they must already have a LexChain
account), then `POST /documents/{id}/parties` `{email, role: "viewer"}` →
party with `status: "pending"`.

Client side: `GET /documents/invitations/mine` →
`POST /documents/invitations/{invitation_id}/accept`. A pending invitation
grants nothing; once accepted the client sees the document, and if a draft
exists Google shares it with them as a commenter (Google sends its own
"shared with you" email).

Invite before or after creating the draft — both work. Removing a party
(`DELETE /documents/{id}/parties/{user_id}`) also unshares the Doc.

### Step 3 — create the Google Docs draft (optional but normal)

Shown while `permissions.can_create_draft` (PREPARING, no draft yet). Offer
three choices:

| Choice | Call |
|---|---|
| Blank | `POST /documents/{id}/draft` `{"source": "blank"}` |
| From template | Google Picker → `{"source": "template", "file_id": "<picked>"}` (LexChain copies it) |
| Choose existing Doc | Google Picker → `{"source": "existing", "file_id": "<picked>"}` (uses it as is) |

Picker token: `GET /google/picker-token`. There is **no paste-a-link option**
by design. After success, `draft_url` is set → show **Open draft** (new tab).

A draft is not required: a lawyer can mark ready without one (e.g. text drafted
elsewhere). Without a draft there are no comments.

### Step 4 — discuss in Google Docs

Lawyer and client work in the Doc itself. LexChain mirrors **comments only**:

- `GET /documents/{id}/comments` → threads with replies and `unresolved` count.
  Read-only — replying and resolving happen in Google Docs.
- `POST /documents/{id}/comments/sync` → pull now ("Refresh comments" button).
  The backend also syncs every ~5 min.
- New comments, replies, resolves and reopens notify the other side in-app
  (`GET /notifications/`, types `DRAFT_COMMENT`, `DRAFT_REPLY`,
  `DRAFT_RESOLVED`, `DRAFT_REOPENED`) and by email.

Required copy:
- For clients, next to the draft link: *"Leave feedback with comments (select
  text → comment button), not by typing in the document."* Typing creates
  suggestions, which LexChain cannot see.
- `author_user_id` may be null — show `author_name`.

### Step 5 — mark ready for signature

Shown while `can_mark_ready`. Above the button, for the lawyer: *"Check the
draft for pending suggestions in Google Docs before marking it ready."*

`POST /documents/{id}/ready`:
- The backend syncs comments first. Any open thread → **409
  `UNRESOLVED_COMMENTS`**, `details.unresolved` = count. Show the count and a
  link to the draft; the stage does not change.
- Success → `READY_FOR_SIGNATURE`. The Doc becomes read-only for everyone
  but the lawyer. Show *"The draft is locked for printing."*

Need a change after this? **Back to preparing** (`can_reopen`) →
`POST /documents/{id}/reopen` → `PREPARING`, Doc editable/commentable again.
Then mark ready again.

### Step 6 — offline: print, sign, notarize

Nothing happens in LexChain. The document waits in `READY_FOR_SIGNATURE`. A
"Waiting for signed copy" badge is appropriate.

### Step 7 — attach the signed copy

Shown while `can_attach_signed_copy` (only from READY). The lawyer scans the
signed, notarized paper and uploads the PDF.

`POST /documents/{id}/signed-copy?book_id=&doc_no=&page_no=` multipart `file`
(PDF, ≤ 60 MB).

- `book_id` required — pick from `GET /books/`.
- `doc_no` / `page_no` = the Doc. No. / Page No. written in the notarial
  block. Optional for an `OPEN` book (next free entry is used), required for a
  `CLOSED` book.
- Success → `SIGNED`, `signed_copy` set, `status: QUEUED`.
- 409 `DUPLICATE_FILE` → this PDF is already filed (`details.document_id` may
  link to it). Other 409s: Doc. No. taken, page full — show `message`.

From here Path 1 and Path 2 are identical → **Confirm and finalize** below.

### Cancel

`can_cancel` in PREPARING or READY → `POST /documents/{id}/cancel` → 
`CANCELLED`. Confirm first. Not possible once signed.

---

## Path 2 — already signed

Use for documents signed outside LexChain: old files, walk-ins, documents from
another lawyer or a bank. No draft, no comments, no clients needed.

### Step 1 — upload

Lawyer clicks **Upload signed document**. Form: name, book (from
`GET /books/`), Doc. No. and Page No. (optional for an OPEN book, required for a
CLOSED one), PDF file.

`POST /documents/upload?file_name=&book_id=&doc_no=&page_no=` multipart
`file` → 201, `lifecycle: SIGNED`, `status: QUEUED`.

### Step 1a — the wrong-path guard (409 `READY_FOR_SIGNATURE_EXISTS`)

If the lawyer has Path 1 documents waiting in `READY_FOR_SIGNATURE`, the
upload is refused — the file may be the signed copy of one of them, uploaded as
a new record by mistake. `details.documents` = `[{document_id, file_name,
parties: [email…]}]`.

Show a dialog: *"Documents are waiting for their signed copy. Did you mean to
attach this file to one of them?"*
- One button per listed document → keep the same file and book/numbers, call
  that document's attach endpoint (Path 1, Step 7).
- **"No, this is a new document"** → resend the same upload with
  `confirm_new_record=true`.

### Step 1b — other refusals

- 409 `DUPLICATE_FILE` → same PDF already filed; link to `details.document_id`.
- 409 Doc. No. taken / page full, 400 bad file / missing numbers for a CLOSED
  book → show `message`.

Then → **Confirm and finalize**.

---

## Both paths — confirm and finalize

### Review the signed copy and its risks (`SIGNED`)

Upload is not finalize. The signed copy is processed right away so the lawyer
sees its risk clauses while it can still be replaced.

Poll `GET /documents/{id}` while `status` is `QUEUED`/`PROCESSING`/`ENRICHING`.

1. `AWAITING_REVIEW` → `GET /documents/{id}/extraction` (blocks + flags);
   edits via `PATCH /documents/{id}/extraction` `{edits: [{index, text}]}`;
   optional `POST …/extraction/analyze`; then `POST …/extraction/approve`.
2. `ENRICHING → COMPLETED` → summary, labels, entities, `risk_flags` appear on
   the detail; `POST /documents/{id}/search` `{query}` and
   `POST /documents/{id}/ask` `{question}` work.
3. `analysis_status: "partial"` → some sections could not be analyzed; warn
   that `risk_flags` may be missing clauses. (A fully failed analysis shows as
   `status: FAILED`, never as an empty risk list.)
4. `FAILED` → `can_retry_processing` → `POST /documents/{id}/retry-processing`.

If a risk clause needs action, replace the signed copy (re-signed). If it is a
false positive or only needs awareness, finalize.

Review edits only the searchable text — never the signed PDF or its on-chain
hash.

Until finalize:

| Flag | Action | Call |
|---|---|---|
| `can_replace_signed_copy` | Wrong file, or a clause needed re-signing → replace | `PUT /documents/{id}/signed-copy` multipart `file` + form `reason` (required). Old copy is kept in history; the old extraction, summary and risk flags are cleared and the new copy is processed from the start (`status: QUEUED`). |
| `can_correct_entry` | Wrong Doc./Page No. or book | `PATCH /documents/{id}/entry` `{doc_no, page_no, book_id?}` |
| `can_rename` | Rename | `PATCH /documents/{id}` `{file_name}` |

Show the PDF (`GET /documents/{id}/file` → short-lived `url`) and the register entry on this
screen so the lawyer can check them. `GET /documents/{id}/signed-copies`
lists every copy, newest first, with `replaced_reason`.

### Finalize

`can_finalize` is `false` until `status` is `COMPLETED` — keep the button
visible but disabled with *"Available once the risk check is complete"*. A
finalize sent too early gets 409 `PROCESSING_NOT_COMPLETE` (`details.status`).

`can_finalize` → confirm: *"This freezes the signed copy and records its
fingerprint on the blockchain. It cannot be undone."* →
`POST /documents/{id}/finalize` → `{document_id, tx_hash, onchain_document_id,
data_hash}`.

Takes ~10–30 s (waits for the chain): show progress, disable the button, never
double-submit. A 503 means the chain was unreachable — the document stays
`SIGNED`; let the lawyer retry.

After success, re-fetch `GET /documents/{id}`: `lifecycle: FINALIZED`,
`on_chain: true`, `status: COMPLETED` (nothing is reprocessed). Link the tx:
`https://sepolia.basescan.org/tx/0x{tx_hash}`.

### Verify

`GET /documents/{id}/verify` → `status`: `AUTHENTIC`, `TAMPERED`,
`SNAPSHOT_COMPROMISED`, `NOT_ANCHORED`, or `VERIFICATION_UNAVAILABLE` (not a
tamper result — retry later). Compares the stored PDF's hash with the one on
chain.

### Search and ask (after finalize)

The analysis from `SIGNED` carries over: summary, risk flags, search and ask
work as before. OCR review is closed once finalized.

---

## Document page — what to show per stage

| Stage | Main buttons (by flag) | Panels |
|---|---|---|
| PREPARING | Create draft ▾ / Open draft · Invite · Mark ready · Rename · Cancel | Parties, Comments (+ Refresh), History |
| READY_FOR_SIGNATURE | Attach signed copy · Back to preparing · Open draft (read-only) · Rename · Cancel | "Waiting for signed copy", Comments (record), History |
| SIGNED | Review OCR (when `AWAITING_REVIEW`) · Finalize (disabled until `COMPLETED`) · Replace signed copy · Correct entry · Rename · Retry (when `FAILED`) | Processing status, risk clauses + analysis, Signed copy preview + register entry, Signed copies, History |
| FINALIZED | Verify · Search · Ask | Tx link, analysis, History |
| CANCELLED | — | History |

History panel: `GET /documents/{id}/history` → `{changes: [{from_stage,
to_stage, action, actor_id, created_at}]}`.

Documents list: `GET /documents/?lifecycle=` — split "My documents"
(`is_owner`) from "Shared with me"; two primary buttons for lawyers: **New
document** (Path 1) and **Upload signed document** (Path 2).

## Client view (both paths)

- Sees only documents whose invitation they accepted; all action flags false.
- Path 1: **Open draft** while PREPARING (comment there), read-only after
  READY. Comment list and notifications about replies/resolves.
- After FINALIZED: detail and verify result.

## Error codes in these flows

| `details.reason` | Where | UI |
|---|---|---|
| `GOOGLE_NOT_CONNECTED` / `GOOGLE_RECONNECT_REQUIRED` | create draft, ready, reopen, sync | Connect Google |
| `UNRESOLVED_COMMENTS` | mark ready | Show `details.unresolved`, link draft |
| `READY_FOR_SIGNATURE_EXISTS` | Path 2 upload | "Attach instead?" dialog |
| `DUPLICATE_FILE` | upload, attach, replace | Link to `details.document_id` if present |
| `PROCESSING_NOT_COMPLETE` | finalize | Finalize waits for processing; show `message`, refresh the detail |

Any other 409 is a stage rule or register conflict — show `message` (e.g.
*"Mark the document Ready for signature before attaching the signed copy."*).
404 = not found **or** not allowed (by design). 403 = a client tried a lawyer
action.
