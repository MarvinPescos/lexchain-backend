# Document lifecycle + Google Docs drafting — context

Status: needs-triage

What was decided, why, and where it lives in code. Written 2026-10-04 on
branch `feat/document-lifecycle` (nothing committed yet). Design reference:
`docs/architecture.md` (Documents section), diagrams in
`docs/gdocs-integration.md`.

## The model

One `Document`, one lifecycle, **two entry points**:

- **Path 1 — prepared with LexChain.** Lawyer opens a record → working draft in
  Google Docs (lawyer and client comment) → *Ready for signature* → printed,
  signed and notarized on paper → signed PDF attached → lawyer finalizes.
- **Path 2 — already signed.** Old files, walk-ins, documents from another
  lawyer or bank: the signed/notarized PDF is uploaded directly → lawyer
  finalizes.

Stages (`Document.lifecycle`, changed only by `lifecycle.transition`):
`PREPARING ⇄ READY_FOR_SIGNATURE → SIGNED → FINALIZED`, plus `CANCELLED`
(only before signing). Path 2 starts at `SIGNED`. Every transition writes a
`DocumentStageChange` row (the history).

## Decisions and why

| Decision | Why |
|---|---|
| Stages are strict: attaching a signed copy is refused from `PREPARING`; LexChain never performs a stage for the lawyer | A law firm: *Ready* is the deliberate "text agreed, stop editing" act. Shortcuts that skip it were rejected. |
| Upload ≠ finalize | The lawyer must confirm the right signed copy is in. Until finalize the copy can be replaced (with a reason) and the entry corrected. |
| Finalize hashes the **PDF bytes**, not OCR text | Finalize happens before OCR now. The signed PDF is the legal document. Verify compares file hashes; the OCR text diff (`tamper.py`) is no longer used. |
| OCR/NLP runs **after** finalize | Nothing is processed until the lawyer confirms. `status` is null until finalize, then `QUEUED → … → COMPLETED`. OCR review only exists on finalized documents. |
| Register entry (Doc. No., Page No.) is claimed when the signed copy is attached | It's written in the notarial block at signing; a Path 1 record has no number while being prepared. |
| Same file can't be filed twice by one lawyer | Unique index on the *current* signed copy's sha256 (replaced copies don't count). |
| Wrong-path guard on Path 2 upload | If documents are waiting at *Ready for signature*, upload is refused (409 `READY_FOR_SIGNATURE_EXISTS`, lists them) unless `confirm_new_record=true`. Catches "uploaded as new instead of attaching". Matched by stage — a Ready record has no Doc. No. yet. |
| Google Doc is a **working copy only** | LexChain stores its link and file id, never its content. Never hashed, stored or OCR'd. |
| No paste-link | Bad UX, and with `drive.file` LexChain can't manage a Doc it didn't create. Drafts are created as blank, copied from a template, or an existing Doc chosen via Google Picker (picking grants access). |
| Per-lawyer Google OAuth, `drive.file` scope | Docs live in the lawyer's own Drive; LexChain sees only files it created or the lawyer picked. A shared service account was rejected (client drafts in LexChain's Drive). |
| `httpx` against Drive REST; no `google-api-python-client` | Async, few endpoints, no new packages (`httpx`, `cryptography` already present). |
| Tokens Fernet-encrypted at rest | A DB dump must not hand out access to lawyers' Drives. |
| Comments are **polled**, not pushed | Drive `files.watch` doesn't report comments and expires within a day; Workspace Events for Drive is a developer preview. Polling every 5 min (`DRAFT_COMMENT_SYNC_SECONDS`), plus on demand and before *Ready*. |
| *Ready* refused while any comment is open | Every question is closed before the text is frozen (409 `UNRESOLVED_COMMENTS`). |
| Suggestions are **not** tracked — comments only (decided 2026-10-07) | Clients get the commenter role; typing in the Doc makes a *suggestion*, which changes nothing until the lawyer accepts it. The Drive comments API doesn't return suggestions or their replies. Rather than read the Doc via the Docs API to block *Ready* on pending suggestions, the lawyer checks the Doc manually before marking ready — the firm prefers a deliberate review over an automated gate. The UI should tell clients to use comments and remind the lawyer to review suggestions. |
| Notify: thread participants + lawyer, minus the actor | Covers client comments → lawyer, lawyer questions → clients, replies/resolve/reopen → the other side. In-app **and** email; duplicating Google's own emails is fine. |

## Google Docs integration — how it works

- **Connect:** `POST /google/connect` → Google consent → `GET /google/callback`
  (no bearer token; encrypted `state` names the lawyer) → redirect to
  `GOOGLE_CONNECTED_REDIRECT?google=connected`. Access tokens refresh
  automatically; if Google revokes, the connection is dropped and the lawyer
  is asked to reconnect (409 `GOOGLE_NOT_CONNECTED`).
- **Create draft:** `POST /documents/{id}/draft` with `source` =
  `blank | template | existing` and `file_id` from the Picker
  (`GET /google/picker-token`). Only while `PREPARING`, once per document.
- **Sharing:** accepted parties get the draft — `viewer`/`signer` →
  commenter, `editor` (another lawyer) → writer. Accepting later shares it
  (read-only if already *Ready*); removing a party unshares. If Google is down
  the LexChain side still succeeds; the Doc catches up on the next lock/unlock.
- **Lock:** *Ready* sets every party to reader (lock first — if Drive refuses,
  the stage doesn't change); *back to preparing* restores roles.
- **Comments:** full fetch each sync (legal drafts have few comments), mirrored
  into `DraftComment` / `DraftReply`. Authors matched by Drive's `me`
  (the lawyer) or `permissionId` (a party's permission). Unmatched authors are
  stored, not notified. Idempotent; the first sync of a Doc imports silently.
  `GET /documents/{id}/comments`, `POST /documents/{id}/comments/sync`.
- **Emails:** queued as `notifications.draft_activity_email` only after the
  sync commits; comment text is HTML-escaped.

## Where it lives

```
app/features/documents/
  constants.py · models.py · repository.py · schemas.py
  lifecycle.py        stage rules (pure)
  access.py           who may do what
  queries.py          list / get / signed copies / history
  presenters.py       detail response, built once
  router.py           mounts extraction/router.py
  workflow/
    intake.py         open, upload signed, attach / replace copy, rename, correct entry
    drafting.py       create draft, ready / reopen (lock), cancel, party draft access
    discussion.py     comment sync, notifications, open-thread count
    sharing.py        parties, invitations — mirrored onto the draft
    finalization.py   freeze + anchor sha256(PDF) + queue OCR
  extraction/         OCR review (own router)
app/features/google/  GoogleConnection, OAuth flow, /google routes
app/infrastructure/google/   OAuth client (httpx) + mock
app/infrastructure/drafts/   DraftWorkspace: Google Docs (httpx) + in-memory mock
app/workers/sync_draft_comments.py · celery task document.sync_draft_comments (beat)
```

Migrations: `86e74d999c7f` (lifecycle, signed copies, history),
`eff79a177033` (Google connections, draft ids), `e23918dc9d39` (draft
comments, notification types).

## Settings

| Setting | Default | Notes |
|---|---|---|
| `USE_REAL_GOOGLE` | `false` | Mock sign-in + in-memory Drive when off |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | — | Web OAuth client |
| `GOOGLE_REDIRECT_URI` | `http://localhost:8000/google/callback` | Must match the OAuth client |
| `GOOGLE_TOKEN_KEY` | — | Fernet key; required with real Google |
| `GOOGLE_CONNECTED_REDIRECT` | `http://localhost:3000/settings` | Frontend page after connecting |
| `DRAFT_COMMENT_SYNC_SECONDS` | `300` | Beat interval |
| `USE_REAL_EMAIL` | `true` | `build_email_provider()` was inverted (prod got the mock); now explicit. Set `false` when testing with made-up addresses. |

## Running and testing

- `docker compose up -d db` (host port **5436**). `.env`'s `TEST_DB_URL`
  points at 5432 — override it or fix `.env`.
- `TEST_DB_URL=postgresql+asyncpg://MarQ:…@localhost:5436/lexChain_test pytest` — 97 tests.
- If the port answers but connections time out, the compose bridge network
  broke (seen after a restart): `docker compose down && docker compose up -d db`.
- Comment sync on a schedule needs the `celery-beat` service (added to
  `docker-compose.yml`).

## Found in the live run (2026-10-06)

- Google's consent screen shows Drive access as an unticked checkbox; a
  connection without `drive.file` is now refused (fixed, `1dccba6`).
- Live-verified against Google: connect, create draft, share to an accepted
  client as commenter, lock on *Ready* (reader), unlock on reopen, comment
  sync, the open-comments gate, resolve.
- A commenter typing in the Doc creates a suggestion — invisible to the sync
  (see the suggestions decision above).
- Drive does not return a commenter's email or permission id (only `me`,
  a profile name and a photo, which don't match the share). Authors are now
  attributed by `me` (the lawyer) or by elimination when the draft is shared
  with exactly one person besides the lawyer and LexChain made that share;
  otherwise unattributed, and replies/resolves on such threads notify every
  client on the document. Verified live with a second Google account.

## Signing half, live on Supabase + Base Sepolia (2026-10-07)

- Verified: Path 2 upload to the real `Documents` bucket (hash matches),
  duplicate refused, finalize (real anchor tx, `FINALIZED`, OCR queued),
  verify `AUTHENTIC`; Path 1 attach-too-early refused, ready, attach,
  replace with reason, correct entry.
- Fixed (`9427a77`): the ABI was only found inside Docker.
- Fixed (`3c17b4f`): the stored anchor record was all zeros — a read-back
  hit a lagging RPC node; now taken from the receipt's event, receipt status
  checked, empty reads retried, RPC failure is a 503.
- Found, not fixed:
  - Finalize no longer archives the original to the backup bucket and the
    restore route is gone; `infrastructure/backup` is unused. (The
    `feat(documents)` commit message says otherwise — it is wrong.)
  - `get_blockchain_provider()` uses the real chain in development and the
    **mock in production** — production would never anchor.
  - `LexChainNotary.storeDocumentHash` overwrites an existing record; it is
    not write-once.

## Not done / next

1. **Commit** — the branch has no commits; ~114 changed paths.
2. **First run against real Google** — only mock and a fake HTTP transport so
   far. Needs Google Cloud: Drive API enabled, consent screen with `openid`,
   `email`, `…/auth/drive.file`, Web OAuth client with the callback URI.
3. **ADR + CONTEXT.md** — anchoring PDF bytes instead of text; two entry
   paths; strict stages; Google Doc as working copy only.

### Parked (after the feature)

- Voiding a `SIGNED` record created by mistake that the wrong-path guard
  didn't catch. Cancel from `SIGNED` is refused until decided.
- Chain anchoring as a background job (`Anchor` PENDING) so an outage doesn't
  block finalize.
- Move verify into `blockchain/` (removes a circular-import workaround;
  needs the blockchain router re-enabled).
- Email preferences (client opt-out of draft emails).
- Google push events via Workspace Events / Pub/Sub (preview).
- `users` model/schema drift (`avatar`, `user_metadata`) — predates this work.
- Invitation email template renders `width="100%%"` literally.
