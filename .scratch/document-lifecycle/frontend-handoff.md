# LexChain — front-end hand-off (documents + Google Docs drafting)

For the front-end agent. Everything here is the backend contract as built on
branch `feat/document-lifecycle`. The live OpenAPI schema is the source of
truth for field details: `GET {BASE_URL}/openapi.json` (Swagger UI at
`{BASE_URL}/docs`).

## Connection

- `BASE_URL`: `https://bb1f-143-44-185-113.ngrok-free.app` (dev tunnel as of
  2026-10-08 — it changes whenever the tunnel restarts; ask for the current one).
- Through ngrok, send header `ngrok-skip-browser-warning: true` on every
  request, or the free tunnel answers with an HTML interstitial.
- CORS allows the origins in the backend's `CORS_ORIGINS` (default
  `http://localhost:3000`, `http://localhost:5173`). If your dev server runs
  elsewhere, ask for it to be added.
- Auth: `Authorization: Bearer <access_token>`. No cookies.

### Auth

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/auth/signup` | `{email, password, f_name, l_name}` | `{message, user_id, email, requires_email_confirmation}` |
| POST | `/auth/signin` | `{email, password}` | `{access_token, refresh_token, token_type, expires_in, user: {id, email, role}}` |
| POST | `/auth/signout` | — (bearer) | `{message}` |
| POST | `/auth/resend-verification` | `{email}` | `{message}` |

`user.role` is `lawyer` or `user` (a client). There is no refresh endpoint:
when a call returns 401, send the user back to sign-in.

### Errors — one shape everywhere

```json
{ "error_type": "ConflictError", "message": "human readable",
  "status_code": 409, "details": { "reason": "MACHINE_CODE" }, "timestamp": "…" }
```

Show `message` to the user. Branch on `details.reason` when present (table at
the end). 422 validation errors carry `details.errors` (per-field list).

## The model the UI must follow

One **document**, two ways in:

- **Path 1 — prepared in LexChain:** lawyer opens a record → works on a
  Google Docs draft with the client → marks it *Ready for signature* → it is
  printed, signed and notarized on paper → lawyer attaches the scanned signed
  PDF → finalizes.
- **Path 2 — already signed:** lawyer uploads a signed/notarized PDF directly
  → finalizes.

`lifecycle` (stage): `PREPARING ⇄ READY_FOR_SIGNATURE → SIGNED → FINALIZED`,
plus `CANCELLED`. Path 2 starts at `SIGNED`.

`status` is the OCR/NLP pipeline and is **null until FINALIZED**, then
`QUEUED → PROCESSING → AWAITING_REVIEW → ENRICHING → COMPLETED` (or `FAILED`).
Show it only on finalized documents ("Processing…", "Needs review", …).

**Never compute what a user may do.** Every document response has
`permissions` — render an action only when its flag is true:

`can_rename, can_create_draft, can_mark_ready, can_reopen,
can_attach_signed_copy, can_replace_signed_copy, can_correct_entry,
can_cancel, can_finalize, can_share, can_revoke` (`can_view` is always true).

Clients (`role: user`) get all action flags false; they read documents they
accepted an invitation to, and comment in Google Docs.

## Screens

### Documents list — `GET /documents/?lifecycle=&limit=&offset=`

Returns an array of
`{id, file_name, lifecycle, status, on_chain, is_owner, my_role, shared_at,
book_id, doc_no, page_no, created_at}`. Split "My documents" (`is_owner`) from
"Shared with me". Two primary buttons for lawyers:

- **New document** (Path 1) → `POST /documents/` `{file_name}` → 201 detail.
- **Upload signed document** (Path 2) → see *Upload* below.

### Document page — `GET /documents/{id}`

Detail: `{document_id, file_name, lifecycle, status, on_chain, draft_url,
signed_copy, document_hash, book_id, doc_no, page_no, finalized_at,
finalized_by, cancelled_at, permissions, summary, labels, entities,
risk_flags, created_at, updated_at}`.
`signed_copy`: `{id, storage_url, sha256, content_type, size_bytes,
original_filename, uploaded_by, replaced_reason, is_current, created_at}`.

Buttons by flag (all lawyer-only; every action returns the updated detail):

| Flag | Button | Call |
|---|---|---|
| `can_create_draft` | Create draft ▾ (Blank / From template / Choose existing Doc) | `POST /documents/{id}/draft` `{source, file_id?}` |
| — (`draft_url` set) | Open draft | open `draft_url` in a new tab |
| `can_mark_ready` | Mark ready for signature | `POST /documents/{id}/ready` |
| `can_reopen` | Back to preparing | `POST /documents/{id}/reopen` |
| `can_attach_signed_copy` | Attach signed copy | `POST /documents/{id}/signed-copy?book_id=&doc_no=&page_no=` multipart `file` |
| `can_replace_signed_copy` | Replace signed copy | `PUT /documents/{id}/signed-copy` multipart `file` + form `reason` (required) |
| `can_correct_entry` | Correct Doc./Page No. | `PATCH /documents/{id}/entry` `{doc_no, page_no, book_id?}` |
| `can_finalize` | Finalize | `POST /documents/{id}/finalize` → `{document_id, tx_hash, onchain_document_id, data_hash}` |
| `can_cancel` | Cancel | `POST /documents/{id}/cancel` |
| `can_rename` | Rename | `PATCH /documents/{id}` `{file_name}` |
| `can_share` | Invite / parties | see *Sharing* |
| `lifecycle == FINALIZED` | Verify | `GET /documents/{id}/verify` |

Confirm before **Finalize** ("This freezes the signed copy and records its
fingerprint on the blockchain. It cannot be undone."). Finalize can take
~10–30 s (it waits for the chain) — show progress, don't double-submit.

Side panels: `GET /documents/{id}/history` →
`{changes: [{from_stage, to_stage, action, actor_id, created_at}]}`;
`GET /documents/{id}/signed-copies` → `{copies: [signed_copy…]}` newest first
(show `replaced_reason`).

### Upload (Path 2) — `POST /documents/upload`

Query: `file_name`, `book_id`, `doc_no?`, `page_no?`, `confirm_new_record?`;
multipart `file` (PDF, ≤ 60 MB). Returns 201 detail in `SIGNED`.

- Doc. No. / Page No. are optional for an OPEN book (next free entry is
  used) and required for a CLOSED book.
- **409 `READY_FOR_SIGNATURE_EXISTS`**: documents are waiting for their signed
  copy. `details.documents` lists them (`document_id, file_name, parties`).
  Show *"Did you mean to attach this to one of these?"* with a button per
  document (→ attach endpoint on that document) and **"No, this is a new
  document"** (→ resend with `confirm_new_record=true`).
- **409 `DUPLICATE_FILE`**: the same file is already filed;
  `details.document_id` → link to it.

### Books — `/books/`

`GET /books/` list, `POST /books/` `{book_number, series_year, status:
"OPEN"|"CLOSED"}`, `GET /books/{id}`, `POST /books/{id}/close`,
`DELETE /books/{id}`. Response `{id, book_number, series_year, status,
closed_at, entry_count, last_doc_no, last_page_no, created_at, updated_at}`.
Needed for the book picker on upload/attach.

### Sharing (lawyer) and invitations (client)

- `GET /documents/{id}/parties` → `{document_id, issuer, parties: [{id,
  user_id, email, f_name, l_name, role, status, responded_at, created_at}]}`.
- `POST /documents/{id}/parties` `{email, role: "viewer"}` → party, `status:
  "pending"`. The email must belong to a LexChain user (`GET /users/search`).
- `DELETE /documents/{id}/parties/{user_id}`.
- Client: `GET /documents/invitations/mine` → `{invitations: [{invitation_id,
  document_id, file_name, role, status, invited_at}], total}`;
  `POST /documents/invitations/{invitation_id}/accept|decline`.

A pending invitation grants nothing. Once accepted, the client sees the
document and, if a draft exists, gets Google's own "shared with you" email.

## Google Docs drafting

### Connect Google (lawyer, once)

1. `GET /google/connection` → `{connected, google_email, connected_at}`.
2. If not connected: `POST /google/connect` → `{authorization_url}`;
   **navigate the whole window** there (not fetch, not an iframe).
3. Google redirects to the backend, which redirects to the front-end page
   configured as `GOOGLE_CONNECTED_REDIRECT` (default
   `http://localhost:3000/settings`) with `?google=connected` or
   `?google=error&message=…`. Read the query param and show the result.
4. Tell the lawyer before redirecting: *"On Google's screen, tick the Google
   Drive permission."* Without it the backend refuses the connection
   (`google=error`).
5. `DELETE /google/connection` disconnects.

Any draft call can answer **409 `GOOGLE_NOT_CONNECTED`** or
**409 `GOOGLE_RECONNECT_REQUIRED`** → show "Connect Google" and restart step 2.

### Create draft

- **Blank:** `POST /documents/{id}/draft` `{"source": "blank"}`.
- **From template / Choose existing Doc:** open the **Google Picker** (Google's
  JS widget) and pass the picked file's id: `{"source": "template" |
  "existing", "file_id": "<id>"}`. Picker setup:
  - OAuth token: `GET /google/picker-token` → `{access_token, expires_at}`
    (scope `drive.file`; picking a file is what grants LexChain access).
  - Client ID: the OAuth client ID (ask); App ID: Google Cloud project
    number `149191550858`; developer key: a Picker API key (ask).
  - Restrict the view to Google Docs (`application/vnd.google-apps.document`).
- Then open `draft_url` in a new tab. Accepted clients are added as
  commenters automatically.

### Comments on the draft

- `GET /documents/{id}/comments` → `{document_id, synced_at, unresolved,
  comments: [{id, author_name, author_user_id, is_lawyer, content,
  quoted_text, resolved, created_at, replies: [{author_name, author_user_id,
  is_lawyer, content, action, created_at}]}]}`. Read-only mirror; replying
  and resolving happen in Google Docs. A reply's `action` of
  `resolve`/`reopen` means it resolved/reopened the thread.
- `POST /documents/{id}/comments/sync` pulls from Google now (backend also
  syncs every ~5 min). Offer a "Refresh comments" button.
- `author_user_id` can be null (Google doesn't reveal who wrote it when
  several clients share a Doc) — show `author_name`.
- **Mark ready is refused while threads are open:** 409
  `UNRESOLVED_COMMENTS` with `details.unresolved`. Show the count and a link
  to the draft.

### Required UI copy (product decisions)

- On the draft link, for clients: *"Leave feedback with comments (select text
  → comment button), not by typing in the document."* Typing creates
  *suggestions*, which LexChain cannot see.
- On the Ready button, for the lawyer: *"Check the draft for pending
  suggestions in Google Docs before marking it ready."*
- After Ready: the Doc is read-only for clients until *Back to preparing*.

## Notifications

`GET /notifications/` → `{notifications: [{id, type, title, body, is_read,
event_metadata, created_at}], total}`; `GET /notifications/unread-count` →
`{unread}`; `PATCH /notifications/{id}/read`; `PATCH /notifications/read-all`.
Poll `unread-count` (e.g. every 30–60 s). Draft types: `DRAFT_COMMENT`,
`DRAFT_REPLY`, `DRAFT_RESOLVED`, `DRAFT_REOPENED`;
`event_metadata.document_id` / `draft_url` for the link.

## After finalize — OCR review (lawyer)

When `status == AWAITING_REVIEW`:
`GET /documents/{id}/extraction` → `{blocks: [{index, editable, type, text,
…}], flags: [{block_index, kind, severity, message, excerpt}], …}`;
`PATCH /documents/{id}/extraction` `{edits: [{index, text}]}`;
`POST /documents/{id}/extraction/analyze` (optional LLM pass);
`POST /documents/{id}/extraction/approve`. Then `ENRICHING → COMPLETED`, and
`POST /documents/{id}/search` `{query}` / `POST /documents/{id}/ask`
`{question}` work. The review edits searchable text only — never the signed
PDF or its on-chain fingerprint.

## Verify — `GET /documents/{id}/verify`

`{status, is_authentic, baseline_trusted, onchain_hash, snapshot_hash,
current_hash, tx_hash, onchain_timestamp, issued_by, finalized_at,
verified_at, message}`. `status`: `AUTHENTIC`, `TAMPERED`,
`SNAPSHOT_COMPROMISED`, `NOT_ANCHORED`, `VERIFICATION_UNAVAILABLE` (not a
tamper result — retry later). Link the tx:
`https://sepolia.basescan.org/tx/0x{tx_hash}`.

## `details.reason` codes to handle

| Code | Status | Meaning → UI |
|---|---|---|
| `READY_FOR_SIGNATURE_EXISTS` | 409 | Path 2 upload while documents await a signed copy → "attach instead?" dialog |
| `DUPLICATE_FILE` | 409 | Same PDF already filed → link to `details.document_id` |
| `UNRESOLVED_COMMENTS` | 409 | Open threads on the draft → show count, link draft |
| `GOOGLE_NOT_CONNECTED` | 409 | Lawyer must connect Google |
| `GOOGLE_RECONNECT_REQUIRED` | 409 | Google access revoked/insufficient → reconnect |

Other 409s are stage rules ("Mark the document Ready for signature before
attaching…") — show `message`. 404 means unknown *or* not allowed (by design).
403 is a client attempting a lawyer action.

## Not available / limits

- No token refresh endpoint; no restore endpoint.
- Suggestions in Google Docs are not tracked (comments only).
- Google "Testing" mode: a lawyer's connection expires after 7 days →
  `GOOGLE_RECONNECT_REQUIRED`; reconnect.
